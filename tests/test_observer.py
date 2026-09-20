from __future__ import annotations

import json
import sqlite3
import threading
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hermes_local_knowledge import implicit, observer, okf, plugin, shadow, shadow_hooks
from hermes_local_knowledge.config import Config, IndexSettings, OKFSettings, VerifiedRoutingSettings

IDS = dict(session_id="session", task_id="task", turn_id="turn", api_request_id="api",
           tool_call_id="call")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    config = Config(tmp_path / "source", tmp_path / "profile", tmp_path / "state",
                    IndexSettings(), okf=OKFSettings(auto_generate=False),
                    verified_routing=VerifiedRoutingSettings(mode="shadow"))
    (config.source_root / "docs").mkdir(parents=True)
    config.hermes_home.mkdir()
    (config.source_root / "docs/atlas.md").write_text("# Atlas restore runbook\nRestore Atlas backup.\n")
    monkeypatch.setattr(observer, "resolve_config", lambda: config)
    monkeypatch.setattr(plugin, "resolve_config", lambda: config)
    monkeypatch.setattr(shadow_hooks, "_wake_supervisor", lambda cfg: False)
    return config


def test_parallel_original_objects_context_and_finalizer(cfg: Config) -> None:
    marker: ContextVar[str] = ContextVar("marker")
    barrier = threading.Barrier(12)
    release = threading.Event()
    seen: list[tuple[str, str, str]] = []

    def consume(kind: str, **p: Any) -> None:
        seen.append((kind, p.get("tool_call_id", ""), marker.get()))

    queue = observer.Observer(consume)

    def run(i: int) -> object:
        marker.set(str(i))
        args = {"raw": "PRIVATE CANARY"}
        result = object()

        def call(actual: Any) -> object:
            assert actual is args
            barrier.wait(timeout=5)
            assert release.wait(5)
            return result

        assert queue.middleware(args, call, tool_name="synthetic", **{**IDS, "tool_call_id": str(i)}) is result
        return result

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(run, i) for i in range(12)]
        # Deterministic entry fence, not a sleep-based expectation.
        with queue._condition:
            assert queue._condition.wait_for(lambda: queue.stats().get("accepted") == 12, timeout=5)
        marker.set("end")
        queue.submit("finalize", {})
        assert not queue.drain(0.01)
        release.set()
        for future in futures:
            future.result(timeout=5)
    assert queue.close(5)
    assert seen[-1] == ("finalize", "", "end")
    assert sorted((call, context) for kind, call, context in seen[:-1]) == sorted((str(i), str(i)) for i in range(12))
    assert queue.stats()["accepted"] == queue.stats()["completed"] == 13


def test_failures_never_rerun_or_change_exception(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a: Any, **k: Any) -> None:
        raise ValueError("PRIVATE FAILURE CANARY")

    queue = observer.Observer(broken)
    calls: list[Any] = []
    original = RuntimeError("original")

    def fail(args: Any) -> Any:
        calls.append(args)
        raise original

    with pytest.raises(RuntimeError) as caught:
        queue.middleware({}, fail, tool_name="synthetic", **IDS)
    assert caught.value is original and len(calls) == 1
    assert queue.drain(5)
    assert queue.stats()["consumer_error"] == 1
    monkeypatch.setattr(observer, "project_call", broken)
    result = object()
    assert queue.middleware({}, lambda args: result, **IDS) is result
    assert queue.close(5)
    assert queue.stats()["projection_error"] == 1


def test_full_drain_closed_replay_and_bounded_dedup(cfg: Config) -> None:
    entered, release = threading.Event(), threading.Event()
    seen: list[dict[str, Any]] = []

    def consume(kind: str, **p: Any) -> None:
        entered.set()
        assert release.wait(5)
        seen.append(p)

    queue = observer.Observer(consume, capacity=1, dedup_capacity=1)
    queue.middleware({}, lambda args: "{}", tool_name="synthetic", **IDS)
    assert entered.wait(5)
    queue.middleware({}, lambda args: "{}", tool_name="synthetic", **IDS)
    assert queue.stats()["full"] == 1
    assert not queue.drain(0)
    release.set()
    assert queue.drain(5)
    queue.middleware({}, lambda args: "{}", tool_name="synthetic", **IDS)
    assert queue.drain(5)
    assert len(seen) == 1 and queue.stats()["duplicate"] == 1
    queue.middleware({}, lambda args: "{}", tool_name="synthetic", **{**IDS, "tool_call_id": "other"})
    assert queue.drain(5)
    assert len(queue._seen) == 1
    assert queue.close(5)
    queue.middleware({}, lambda args: "{}", tool_name="synthetic", **IDS)
    assert queue.stats()["closed"] == 1
    assert queue.stats()["accepted"] == queue.stats()["completed"] == 3


def test_projection_privacy_schema_shape_and_oversize(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    schema = {"parameters": {"type": "object", "properties": {"query": {"type": "string", "default": "SECRET"}}}}
    monkeypatch.setattr(okf, "_tool_metadata", lambda name: ("synthetic", schema))
    args: dict[str, Any] = {"query": "Atlas restore", "limit": 3, "untrusted-key": {"secret": "SECRET"}}
    payload = observer.project_call(args, {**IDS, "tool_name": "knowledge_search"})
    observer.project_result(payload, json.dumps({"success": True, "usage_event_id": 1, "body": "SECRET"}), False)
    assert "SECRET" not in json.dumps(payload)
    assert "untrusted-key" not in json.dumps(payload)
    assert payload["_okf_capture"]["arg_shape"] == okf.safe_arg_shape(args)
    assert payload["_okf_capture"]["schema_hash"] == okf.schema_hash(schema)
    source_path = str((cfg.source_root / "docs" / "atlas.md").resolve())
    for name, args, raw in (("read_file", {"path": source_path}, {"content": "SECRET"}),
                            ("skill_view", {}, {"success": True, "_source_path": source_path, "content": "SECRET"})):
        p = observer.project_call(args, {"tool_name": name})
        observer.project_result(p, json.dumps(raw), False)
        assert implicit._file_consumer_path(name, p["args"], p["result"]) == source_path
        assert "SECRET" not in json.dumps(p)
    queue = observer.Observer(lambda *a, **k: None)
    queue.submit("pre", {"oversized": "x" * observer.MAX_RECEIPT_BYTES})
    assert queue.close(5) and queue.stats()["oversize"] == 1


@pytest.mark.parametrize("location", ["properties", "default"])
def test_shared_schema_work_is_bounded(cfg: Config, monkeypatch: pytest.MonkeyPatch, location: str) -> None:
    # Tiny shared input, exponentially large expanded tree. `default` also proves
    # that fields ignored by projection cannot reach the raw-schema hash.
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(12):
        schema = {location: {f"field_{i}": schema for i in range(16)}}
    monkeypatch.setattr(okf, "_tool_metadata", lambda name: ("synthetic", schema))

    unbounded_calls: list[bool] = []

    def unbounded(*args: Any, **kwargs: Any) -> Any:
        unbounded_calls.append(True)
        raise ValueError("oversized schema reached projection/hash")

    monkeypatch.setattr(okf, "project_routing_schema", unbounded)
    monkeypatch.setattr(okf, "schema_hash", unbounded)
    received: list[Any] = []
    queue = observer.Observer(lambda *a, **k: received.append(k))
    calls: list[Any] = []
    args = {"value": "PRIVATE"}
    result = object()

    def downstream(actual: Any) -> object:
        calls.append(actual)
        return result

    try:
        assert queue.middleware(args, downstream, tool_name="synthetic", **IDS) is result
    finally:
        assert queue.close(5)
    assert len(calls) == 1 and calls[0] is args
    assert unbounded_calls == []
    assert received == []
    assert queue.stats()["projection_error"] == 1


@pytest.mark.parametrize("schema", [
    {"enum": [None] * 4096},
    {"description": "x" * observer.MAX_RECEIPT_BYTES},
    {"default": 1 << 256},
])
def test_schema_snapshot_rejects_total_work(schema: Any) -> None:
    with pytest.raises(ValueError):
        observer._schema_snapshot(schema)


def test_schema_snapshot_preserves_shared_json_and_rejects_cycles() -> None:
    child: dict[str, Any] = {"type": "string", "default": "PRIVATE"}
    schema = {"properties": {"first": child, "second": child}}
    copied = observer._schema_snapshot(schema)
    assert copied == schema
    assert okf.schema_hash(copied) == okf.schema_hash(schema)
    assert copied["properties"]["first"] is not copied["properties"]["second"]
    child["items"] = child
    with pytest.raises(ValueError, match="budget"):
        observer._schema_snapshot(schema)


@pytest.mark.parametrize("kind", ["mapping", "sequence"])
def test_arg_shape_reads_only_bounded_prefix(kind: str) -> None:
    from collections.abc import Sequence

    reads: list[int] = []

    class WideMapping(UserDict):
        def items(self) -> Any:
            for index in range(10000):
                reads.append(index)
                yield str(index), "PRIVATE"

    class WideSequence(Sequence):
        def __len__(self) -> int:
            return 10000

        def __getitem__(self, index: Any) -> Any:
            if index >= 10000:
                raise IndexError
            reads.append(index)
            return "PRIVATE"

    value = WideMapping(dict.fromkeys(range(10000))) if kind == "mapping" else WideSequence()
    shape = okf.safe_arg_shape(value)
    assert reads == list(range(okf.DEFAULT_MAX_ARG_ITEMS))
    assert shape["truncated"] is True
    assert shape["field_count" if kind == "mapping" else "length"] == 10000
    assert "PRIVATE" not in json.dumps(shape)


def test_arg_shape_global_budget_drops_observation(cfg: Config) -> None:
    reads: list[int] = []

    class SharedMapping(UserDict):
        def items(self) -> Any:
            for index, child in super().items():
                reads.append(index)
                yield index, child

    tree: Any = "PRIVATE"
    for _ in range(6):
        tree = SharedMapping(dict.fromkeys(range(8), tree))
    args = {"tree": tree}
    received: list[Any] = []
    queue = observer.Observer(lambda *a, **k: received.append(k))
    calls: list[Any] = []
    result = object()

    def downstream(actual: Any) -> Any:
        calls.append(actual)
        return result

    assert queue.middleware(args, downstream, tool_name="synthetic", **IDS) is result
    assert queue.close(5)
    assert len(reads) <= 256
    assert len(calls) == 1 and calls[0] is args
    assert received == []
    assert queue.stats()["projection_error"] == 1
    assert queue.stats()["discarded"] == 1


@pytest.mark.parametrize("tool_name", ["synthetic", "knowledge_search", "knowledge_get", "skill_view", "read_file"])
def test_large_result_is_not_parsed(cfg: Config, monkeypatch: pytest.MonkeyPatch, tool_name: str) -> None:
    raw = json.dumps({"success": True, "content": "PRIVATE" * observer.MAX_RESULT_CHARS})
    parsed_large: list[bool] = []
    loads = json.loads

    def spy(value: Any, *args: Any, **kwargs: Any) -> Any:
        if value is raw:
            parsed_large.append(True)
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", spy)
    received: list[Any] = []
    queue = observer.Observer(lambda *a, **k: received.append(k))
    calls: list[Any] = []
    args: dict[str, Any] = {}

    def downstream(actual: Any) -> str:
        calls.append(actual)
        return raw

    assert queue.middleware(args, downstream, tool_name=tool_name, **IDS) is raw
    assert queue.close(5)
    assert parsed_large == []
    assert len(calls) == 1 and calls[0] is args
    assert received == []
    assert queue.stats()["projection_error"] == 1
    assert queue.stats()["discarded"] == 1


@pytest.mark.parametrize("extra", [0, 1])
def test_result_parse_budget_boundary(extra: int) -> None:
    raw = '{"success": false, "error": "failure", "content": ""}'
    raw = raw[:-2] + "x" * (observer.MAX_RESULT_CHARS - len(raw) + extra) + raw[-2:]
    payload: dict[str, Any] = {"tool_name": "read_file"}
    if extra:
        with pytest.raises(ValueError, match="budget"):
            observer.project_result(payload, raw, False)
    else:
        observer.project_result(payload, raw, False)
        assert payload["status"] == "error"
        assert payload["error_type"] == "tool_error"
        assert json.loads(payload["result"]) == {"success": False, "content": ""}


@pytest.mark.parametrize("failure", ["construction", "start"])
def test_thread_startup_failure_is_fail_open_and_recovers(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    ctx = Context()
    plugin.register(ctx)
    queue = ctx.middleware.__self__
    monkeypatch.setattr(plugin, "check_knowledge_available", lambda: False)
    received: list[str] = []
    queue.consume = lambda kind, **p: received.append(kind)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("PRIVATE startup failure")

    with monkeypatch.context() as patch:
        if failure == "construction":
            patch.setattr(observer.threading, "Thread", broken)
        else:
            patch.setattr(observer.threading.Thread, "start", broken)
        for name in ("pre_llm_call", "on_session_end", "on_session_finalize"):
            assert ctx.hooks[name](**IDS) is None
        result = object()
        assert ctx.middleware({}, lambda args: result, tool_name="synthetic", **IDS) is result
        assert queue.stats()["pending"] == 0
        assert queue.stats().get("accepted", 0) == 0
        assert queue.stats()["enqueue_error"] == 4
        assert queue._thread is None
    assert "enqueue_error" in caplog.text
    assert "PRIVATE" not in caplog.text
    queue.submit("pre", {})
    assert queue.close(5)
    assert received == ["pre"]
    assert queue.stats()["accepted"] == queue.stats()["completed"] == 1


class Context:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}
        self.middleware: Any = None

    def register_tool(self, **kwargs: Any) -> None:
        pass

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback

    def register_middleware(self, name: str, callback: Any) -> None:
        assert name == "tool_execution"
        self.middleware = callback


def test_pre_hook_config_failure_omits_shadow_capture(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    def malformed() -> Config:
        raise ValueError("PRIVATE malformed config")

    monkeypatch.setattr(plugin, "resolve_config", malformed)
    monkeypatch.setattr(observer, "resolve_config", malformed)
    ctx = Context()
    plugin.register(ctx)
    queue = ctx.middleware.__self__
    submitted: list[dict[str, Any]] = []
    submit = queue.submit

    def record(kind: str, payload: dict[str, Any]) -> None:
        submitted.append(payload)
        submit(kind, payload)

    monkeypatch.setattr(queue, "submit", record)
    try:
        assert ctx.hooks["pre_llm_call"](**IDS, user_message="PRIVATE request") is None
    finally:
        assert queue.close(5)
    assert len(submitted) == 1
    assert "user_message" not in submitted[0]
    assert "PRIVATE" not in json.dumps(submitted)
    assert queue.stats()["config_error"] == 1
    assert not cfg.state_dir.exists()


@pytest.mark.parametrize("consumer", ["knowledge_get", "read_file", "skill_view"])
def test_registered_full_consumers_and_lifecycle(cfg: Config, consumer: str) -> None:
    skill_path = cfg.source_root / "custom_skills/atlas/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: atlas\ndescription: Atlas restore runbook\n---\n# Atlas restore runbook\nRestore Atlas backup.\n")
    ctx = Context()
    plugin.register(ctx)
    assert "post_tool_call" not in ctx.hooks
    queue = ctx.middleware.__self__
    ctx.hooks["pre_llm_call"](**IDS, user_message="Locate Atlas restore runbook")
    args = {"query": "Atlas restore runbook", "limit": 3, "lookup": {"intent": "Locate restore"},
            "artifact_type": "skill" if consumer == "skill_view" else "runbook"}
    result = ctx.middleware(args, lambda a: plugin._handle_search(a, **IDS), tool_name="knowledge_search", **IDS)
    parsed = json.loads(result)
    artifact = parsed["results"][0]
    if consumer == "knowledge_get":
        consume_args = {"artifact_id": artifact["id"]}
        def downstream(a: dict[str, Any]) -> str:
            return plugin._handle_get(a, **IDS)
    else:
        consume_args = {"path": str(skill_path if consumer == "skill_view" else cfg.source_root / "docs/atlas.md")}
        def downstream(a: dict[str, Any]) -> str:
            return json.dumps({"success": True, "content": "PRIVATE BODY", "_source_path": a["path"]})
    call_ids = {**IDS, "api_request_id": "later-api", "tool_call_id": "consume"}
    ctx.middleware(consume_args, downstream, tool_name=consumer, **call_ids)
    # Replay receipt, not tool execution: all consumer counters must remain stable.
    p = observer.project_call(args, {**IDS, "tool_name": "knowledge_search"})
    observer.project_result(p, result, False)
    queue.submit("post", p)
    ctx.hooks["on_session_end"](**IDS)
    ctx.hooks["on_session_finalize"](**IDS)
    assert queue.close(10)
    with sqlite3.connect(cfg.state_dir / "usage.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM implicit_feedback").fetchone()[0] == 1
    with sqlite3.connect(cfg.state_dir / "okf_queue.sqlite") as conn:
        row = conn.execute("SELECT use_count, arg_shape_json FROM okf_candidates WHERE tool_name='knowledge_search'").fetchone()
        assert row[0] == 1
        assert json.loads(row[1]) == okf.safe_arg_shape(args)
    with shadow._connect(cfg) as conn:
        row = conn.execute("SELECT seen, ready, user_request FROM cases").fetchone()
        assert tuple(row) == (1, 1, "Locate Atlas restore runbook")
    assert queue.stats()["duplicate"] == 1


@pytest.mark.parametrize("lookup, valid", [
    pytest.param([], False, id="empty-list"),
    pytest.param(["PRIVATE"], False, id="list"),
    pytest.param("PRIVATE", False, id="string"),
    pytest.param(False, False, id="false"),
    pytest.param(0, False, id="zero"),
    pytest.param(UserDict({"intent": "PRIVATE"}), False, id="non-dict-mapping"),
    pytest.param({"unknown": "PRIVATE"}, False, id="unknown-key"),
    *[pytest.param({key: value}, False, id=f"{key}-{label}")
      for key, limit in shadow.LOOKUP_LIMITS.items()
      for label, value in (("null", None), ("bool", True), ("number", 1),
                           ("list", ["PRIVATE"]), ("dict", {"secret": "PRIVATE"}),
                           ("blank", " \t"), ("oversize", "PRIVATE" + "x" * limit),
                           ("padded-oversize", "x" * limit + " "))],
    pytest.param(None, True, id="null"),
    pytest.param({}, True, id="empty-dict"),
    pytest.param({"intent": " Locate Atlas restore "}, True, id="trimmed"),
    pytest.param({key: "x" * limit for key, limit in shadow.LOOKUP_LIMITS.items()},
                 True, id="exact-limits"),
])
def test_registered_shadow_lookup_validation(cfg: Config, lookup: Any, valid: bool) -> None:
    # Exercise the real consumer and telemetry join, not only the projection.
    expected: dict[str, str] = {}
    if valid:
        expected = shadow._lookup_context(lookup)["lookup"]["fields"]
    else:
        with pytest.raises(ValueError, match="invalid_lookup_context"):
            shadow._lookup_context(lookup)
    ctx = Context()
    plugin.register(ctx)
    queue = ctx.middleware.__self__
    ctx.hooks["pre_llm_call"](**IDS, user_message="Locate Atlas restore runbook")
    args = {"query": "Atlas restore runbook", "lookup": lookup}
    result = ctx.middleware(args, lambda a: plugin._handle_search(a, **IDS),
                            tool_name="knowledge_search", **IDS)
    assert json.loads(result)["results"]
    ctx.hooks["on_session_end"](**IDS)
    assert queue.close(10)
    assert queue.stats()["delivered"] == 3
    assert not queue.stats().get("consumer_error")
    with shadow._connect(cfg) as conn:
        cases = conn.execute("SELECT lookup_context FROM cases").fetchall()
        skipped = conn.execute("SELECT value FROM counters WHERE name='skipped_invalid_lookup_context'").fetchone()
    if valid:
        assert len(cases) == 1 and skipped is None
        assert json.loads(cases[0][0])["lookup"]["fields"] == expected
    else:
        assert not cases
        assert skipped is not None and skipped[0] == 1
        payload = observer.project_call(args, {**IDS, "tool_name": "knowledge_search"})
        assert payload["args"]["lookup"] is False
        assert "PRIVATE" not in json.dumps(payload)


def test_profile_settings_snapshot_and_session_isolation(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    active: ContextVar[Config] = ContextVar("active", default=cfg)
    monkeypatch.setattr(observer, "resolve_config", lambda: active.get())
    from hermes_local_knowledge.config import resolve_config
    results: list[tuple[Config, str]] = []
    queue = observer.Observer(lambda kind, **p: results.append((resolve_config(), p["session_id"])))
    first = queue.reserve("post")
    assert first is not None
    other = replace(cfg, hermes_home=cfg.hermes_home / "other", okf=OKFSettings(enabled=False))
    active.set(other)
    second = queue.reserve("post")
    assert second is not None
    for slot, session in ((first, "first"), (second, "second")):
        p = observer.project_call({}, {**IDS, "tool_name": "synthetic", "session_id": session})
        observer.project_result(p, "{}", False)
        queue.finish(slot, p)
    assert queue.close(5)
    assert results == [(cfg, "first"), (other, "second")]


def test_immutable_receipt_and_failed_replay(cfg: Config) -> None:
    received: list[dict[str, Any]] = []
    queue = observer.Observer(lambda kind, **p: received.append(p))
    gate = queue.reserve("pre")
    assert gate is not None
    args: dict[str, Any] = {"query": "Atlas restore", "lookup": {"intent": "Original intent"}}
    result = json.dumps({"usage_event_id": 1, "success": False, "error": "PRIVATE ERROR"})
    queue.middleware(args, lambda a: result, tool_name="knowledge_search", **IDS)
    args["lookup"]["intent"] = "Caller mutated"
    queue.middleware(args, lambda a: result, tool_name="knowledge_search", **IDS)
    queue.finish(gate, None)
    assert queue.close(5)
    assert len(received) == 1
    assert received[0]["args"]["lookup"] == {"intent": "Original intent"}
    assert received[0]["status"] == "error"
    assert "PRIVATE ERROR" not in json.dumps(received)
    assert queue.stats()["duplicate"] == 1


def test_okf_error_schema_parity_and_unkeyed_limit(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    schema = {"parameters": {"type": "object", "properties": {
        f"field_{i}": {"type": "string", "default": "PRIVATE"} for i in range(100)}}}
    monkeypatch.setattr(okf, "_tool_metadata", lambda name: ("synthetic", schema))
    queue = observer.Observer(lambda kind, **p: okf._on_post_tool_call(**p))
    args = {"command": "PRIVATE"}
    for _ in range(2):
        queue.middleware(args, lambda a: '{"error":"PRIVATE"}', tool_name="synthetic")
    assert queue.close(5)
    with sqlite3.connect(cfg.state_dir / "okf_queue.sqlite") as conn:
        row = conn.execute("SELECT use_count, success_count, error_count, schema_json, schema_hash FROM okf_candidates").fetchone()
    assert row[:3] == (2, 0, 2)
    assert json.loads(row[3]) == okf.project_routing_schema(schema)
    assert row[4] == okf.schema_hash(schema)
    assert queue.stats()["unkeyed"] == 2
