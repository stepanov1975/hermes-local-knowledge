from __future__ import annotations

import importlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_local_knowledge import applicability, index, observer, plugin, shadow, shadow_hooks
from hermes_local_knowledge.config import Config, IndexSettings, OKFSettings, VerifiedRoutingSettings
from hermes_local_knowledge.routing import ROUTING_TRACE_METADATA_KEY, RouteDecision, RouteOutcome
from hermes_local_knowledge.service import LocalKnowledgeService

QUERY = "Atlas restore runbook"
REQUEST = "Locate the Atlas restore runbook for the primary server."
IDS = dict(session_id="session", task_id="task", turn_id="turn", api_request_id="api", tool_call_id="call")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    root, home, state = (tmp_path / n for n in ("source", "profile", "state"))
    (root / "docs").mkdir(parents=True)
    home.mkdir()
    for n in range(3):
        (root / "docs" / f"restore-{n}.md").write_text(
            f"# Atlas restore runbook {n}\nThis procedure targets the secondary Atlas server only.\n")
    (root / "docs" / "quartz.md").write_text(
        "# Quartz backup procedure\nThis procedure targets only Quartz backups, not Atlas restores.\n")
    config = Config(root, home, state, IndexSettings(), okf=OKFSettings(auto_generate=False),
                    verified_routing=VerifiedRoutingSettings(mode="veto"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in ("LOCAL_KNOWLEDGE_ROOT", "LOCAL_KNOWLEDGE_STATE_DIR",
                 shadow_hooks.SHADOW_WORKER_ENV, shadow_hooks.OKF_WORKER_ENV):
        monkeypatch.delenv(name, raising=False)
    index.build_index(root, state, home, config.index_settings)
    for module in (plugin, observer, shadow_hooks):
        monkeypatch.setattr(module, "resolve_config", lambda: config)
    monkeypatch.setattr(shadow_hooks, "_wake_supervisor", lambda cfg: False)
    with shadow_hooks._LOCK:
        shadow_hooks._REQUESTS.clear()
    yield config
    with shadow_hooks._LOCK:
        shadow_hooks._REQUESTS.clear()


def endorse(cfg: Config, artifact_id: str) -> int:
    return LocalKnowledgeService(cfg).feedback(rating="useful", event_id=None, query=QUERY,
                                         artifact_id=artifact_id, note="", context={})[0]


def prepare(cfg: Config, *, retry: bool = False) -> tuple[list[dict[str, Any]], int, str]:
    baseline = index.search_index(cfg.state_dir / "index.sqlite", QUERY, limit=2, artifact_type="runbook")
    target = "runbook:docs-quartz" if retry else baseline[-1]["id"]
    return baseline, endorse(cfg, target), target


def queue(cfg: Config) -> observer.Observer:
    shadow_hooks.on_pre_llm_call(**IDS, user_message=REQUEST)
    return observer.Observer(lambda kind, **payload: shadow_hooks.on_post_tool_call(**payload))


def search(q: observer.Observer, *, args: dict[str, Any] | None = None,
           ids: dict[str, str] | None = None) -> dict[str, Any]:
    call_ids = ids or {**IDS, "tool_call_id": f"call-{q.stats().get('accepted', 0)}"}
    result = q.middleware(args or {"query": QUERY, "limit": 2, "artifact_type": "runbook"},
                          lambda a: plugin._handle_search(a, session_id="session", task_id="task"),
                          tool_name="knowledge_search", **call_ids)
    assert q.drain(5)
    assert shadow_hooks._SEARCH_SCOPE.get() is None
    return json.loads(result)


def cases(cfg: Config) -> list[dict[str, Any]]:
    with shadow._connect(cfg) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM cases")]


class Model:
    def __init__(self, verdict: str = "inapplicable", *, citations: bool = True,
                 basis: str = "scope_target_incompatibility") -> None:
        self.verdict, self.citations, self.basis = verdict, citations, basis
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        assert kwargs["purpose"].endswith("applicability")
        packet = json.loads(kwargs["input"][0]["text"])
        promotion = packet["lookup_context"]["promotion"]
        assert packet["user_request"] == REQUEST
        assert len(packet["sources"]) == 1
        source = packet["sources"][0]
        assert source["id"] == promotion["artifact_id"]
        return SimpleNamespace(parsed={"verdict": self.verdict, "basis": self.basis,
                                      "citations": [{**{k: source[k] for k in ("id", "locator", "sha256")},
                                                     "start_line": source.get("start_line", 1),
                                                     "end_line": source.get("start_line", 1) + 1}]
                                      if self.citations else []}, usage={})


def complete(cfg: Config, model: Model | None = None) -> dict[str, Any]:
    shadow.finish_session(cfg, "session")
    return shadow.run_batch(cfg, llm=model or Model())


@pytest.mark.parametrize("retry", [False, True])
def test_exact_veto_restores_entire_filtered_page_and_truthful_telemetry(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, retry: bool,
) -> None:
    baseline, feedback_id, target = prepare(cfg, retry=retry)
    q = queue(cfg)
    try:
        initial = search(q)
        assert initial["results"][0]["id"] == target
        captured = json.loads(cases(cfg)[0]["lookup_context"])["promotion"]
        assert captured["baseline_ids"] == [r["id"] for r in baseline]
        assert captured["feedback_id"] == feedback_id and captured["limit"] == 2
        assert (target not in captured["baseline_ids"]) == retry
        model = Model()
        assert complete(cfg, model) == {"claimed": 1, "verified": 1, "unresolved": 0}
        assert len(model.calls) == 1
        monkeypatch.setattr(shadow, "_call", lambda *a, **k: pytest.fail("inline inference"))
        final = search(q)
        assert [r["id"] for r in final["results"]] == [r["id"] for r in baseline]
        with sqlite3.connect(cfg.state_dir / "usage.sqlite") as conn:
            row = conn.execute("SELECT route_outcome,route_feedback_id,route_artifact_id,"
                               "feedback_max_id,baseline_top_ids_json,top_ids_json "
                               "FROM usage_events WHERE id=?", (final["usage_event_id"],)).fetchone()
        assert row[:4] == ("applicability_vetoed", feedback_id, target, feedback_id)
        assert json.loads(row[4]) == json.loads(row[5]) == [r["id"] for r in baseline]
        # Direct/caller-owned access lacks host authority and keeps ordinary retrievability.
        assisted, meta = LocalKnowledgeService(cfg).search(QUERY, limit=2, artifact_type="runbook", ensure=False)
        assert assisted[0]["id"] == target
        assert meta[ROUTING_TRACE_METADATA_KEY].decision.outcome in {
            RouteOutcome.PROMOTED_EXISTING, RouteOutcome.PROMOTED_RETRY}
        owned, owned_meta = LocalKnowledgeService(cfg).search(
            QUERY, limit=2, artifact_type="runbook", ensure=False,
            db_path=cfg.state_dir / "index.sqlite")
        assert [r["id"] for r in owned] == [r["id"] for r in baseline]
        assert owned_meta[ROUTING_TRACE_METADATA_KEY].decision.outcome == RouteOutcome.NONE
        assert index.get_artifact(cfg.state_dir / "index.sqlite", target) is not None
    finally:
        q.close(5)


@pytest.mark.parametrize("verdict", ["applicable", "uncertain"])
def test_completed_non_veto(cfg: Config, verdict: str) -> None:
    _, _, target = prepare(cfg)
    q = queue(cfg)
    try:
        search(q)
        assert complete(cfg, Model(verdict, citations=verdict != "uncertain"))["verified"] == 1
        assert search(q)["results"][0]["id"] == target
        assert cases(cfg)[0]["status"] == "ai_verified"
        assert not shadow.has_work(cfg)
    finally:
        q.close(5)


@pytest.mark.parametrize("invalid", ["uncited", "absence"])
def test_inapplicability_requires_read_citations_and_positive_incompatibility(cfg: Config, invalid: str) -> None:
    _, _, target = prepare(cfg)
    q = queue(cfg)
    try:
        search(q)
        model = Model(citations=invalid != "uncited", basis="absence" if invalid == "absence"
                      else "scope_target_incompatibility")
        assert complete(cfg, model)["unresolved"] == 1
        assert search(q)["results"][0]["id"] == target
    finally:
        q.close(5)


@pytest.mark.parametrize("change", ["bytes", "ttl", "legacy", "corrupt", "citation", "metadata"])
def test_unusable_evidence_fails_open_and_requeues(cfg: Config, change: str) -> None:
    _, _, target = prepare(cfg)
    q = queue(cfg)
    try:
        search(q)
        complete(cfg)
        row = cases(cfg)[0]
        if change == "bytes":
            artifact = index.get_artifact(cfg.state_dir / "index.sqlite", target)
            assert artifact is not None
            path = cfg.source_root / artifact["path"]
            path.write_text(path.read_text() + "Harmless new heading.\n")
        elif change == "metadata":
            with sqlite3.connect(cfg.state_dir / "index.sqlite") as conn:
                conn.execute("UPDATE artifacts SET summary=summary || ' changed' WHERE id=?", (target,))
        else:
            receipt = json.loads(row["result"])
            if change == "legacy":
                receipt = {"contract_version": 2, "baseline_review": [{"disposition": "not_useful"}]}
            elif change == "citation":
                receipt["citations"][0]["sha256"] = "invalid"
            with shadow._connect(cfg, create=True) as conn:
                conn.execute("UPDATE cases SET result=?,verified_at=? WHERE id=?",
                             ("not json" if change == "corrupt" else json.dumps(receipt),
                              time.time() - 31 * 86400 if change == "ttl" else row["verified_at"], row["id"]))
        assert search(q)["results"][0]["id"] == target
        assert any(r["status"] == "pending" for r in cases(cfg)), cases(cfg)
    finally:
        q.close(5)


@pytest.mark.parametrize("change", ["feedback", "query", "limit", "filter", "lookup", "task"])
def test_exact_binding_does_not_transfer_veto(cfg: Config, change: str) -> None:
    _, _, target = prepare(cfg)
    q = queue(cfg)
    try:
        search(q)
        complete(cfg)
        args: dict[str, Any] = {"query": QUERY, "limit": 2, "artifact_type": "runbook"}
        if change == "feedback":
            endorse(cfg, target)
        elif change == "query":
            args["query"] = QUERY.lower()
        elif change == "limit":
            args["limit"] = 3
        elif change == "filter":
            args.pop("artifact_type")
        elif change == "lookup":
            args["lookup"] = {"intent": "Locate a different target."}
        else:
            shadow_hooks.on_pre_llm_call(**IDS, user_message=REQUEST + " Include the secondary server.")
        assert search(q, args=args)["results"][0]["id"] == target
        assert any(r["status"] == "pending" for r in cases(cfg)), cases(cfg)
    finally:
        q.close(5)


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_off_and_shadow_parity_no_veto(cfg: Config, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    _, _, target = prepare(cfg)
    config = replace(cfg, verified_routing=VerifiedRoutingSettings(mode=mode))
    for module in (plugin, observer, shadow_hooks):
        monkeypatch.setattr(module, "resolve_config", lambda: config)
    monkeypatch.setattr(applicability, "veto", lambda *a: pytest.fail("cache read outside veto"))
    q = queue(config)
    try:
        assert search(q)["results"][0]["id"] == target
        assert shadow_hooks._SEARCH_SCOPE.get() is None
        if mode == "off":
            assert not shadow._path(config).exists()
            assert not shadow_hooks._REQUESTS
    finally:
        q.close(5)


def test_already_first_and_implicit_never_cancelled(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = index.search_index(cfg.state_dir / "index.sqlite", QUERY, limit=2)
    endorse(cfg, baseline[0]["id"])
    q = queue(cfg)
    try:
        monkeypatch.setattr(applicability, "veto", lambda *a: pytest.fail("already first is not a promotion"))
        assert search(q)["results"][0]["id"] == baseline[0]["id"]
        assert not shadow._path(cfg).exists()
        decision = RouteDecision(baseline, RouteOutcome.PROMOTED_EXISTING, None, baseline[0]["id"], 0)
        assert applicability.apply(cfg, baseline, decision, query=QUERY, artifact_type="", limit=2) is decision
    finally:
        q.close(5)


@pytest.mark.parametrize("mode", ["off", "shadow", "veto"])
def test_mode_config_accepts_exact_documented_values(mode: str) -> None:
    from hermes_local_knowledge.config import _resolve_verified_routing_settings

    assert _resolve_verified_routing_settings({"verified_routing": {"mode": mode}}).mode == mode


def test_large_source_location_excerpt_and_whole_file_invalidation(cfg: Config) -> None:
    baseline, _, target = prepare(cfg)
    artifact = index.get_artifact(cfg.state_dir / "index.sqlite", target)
    assert artifact is not None
    path = cfg.source_root / artifact["path"]
    path.write_text("# Atlas restore runbook\n" + "Background detail. " * 60 + "\n" +
                    ("Unrelated background line.\n" * 1200) +
                    "Scope: secondary Atlas server only.\n")

    class ExcerptModel(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            packet = json.loads(kwargs["input"][0]["text"])
            artifact_id = packet["lookup_context"]["promotion"]["artifact_id"]
            if not self.calls:
                assert not packet["sources"]
                assert packet["read_refusals"][artifact_id] == "source_too_large"
                self.calls.append(kwargs)
                return SimpleNamespace(parsed={"action": "locate_source", "id": artifact_id,
                                               "query": "Scope:"}, usage={})
            if len(self.calls) == 1:
                self.calls.append(kwargs)
                line = packet["locations"][0]["matches"][0]["line"]
                return SimpleNamespace(parsed={"action": "read_excerpt", "id": artifact_id,
                                               "start_line": line, "end_line": line}, usage={})
            self.calls.append(kwargs)
            source = packet["sources"][0]
            assert source["complete"] is False
            assert source["lines"] == [f'{source["start_line"]}: Scope: secondary Atlas server only.']
            return SimpleNamespace(parsed={"verdict": "inapplicable", "basis": "scope_target_incompatibility",
                                           "citations": [{**{k: source[k] for k in ("id", "locator", "sha256")},
                                                          "start_line": source["start_line"],
                                                          "end_line": source["end_line"]}]}, usage={})

    q = queue(cfg)
    try:
        search(q)
        model = ExcerptModel()
        assert complete(cfg, model)["verified"] == 1
        assert len(model.calls) == 3
        assert [r["id"] for r in search(q)["results"]] == [r["id"] for r in baseline]
        path.write_text(path.read_text().replace("Background detail.", "Edited background.", 1))
        assert search(q)["results"][0]["id"] == target
        assert cases(cfg)[0]["status"] == "pending"
    finally:
        q.close(5)


def test_skill_directory_locator_reads_actual_skill_file(cfg: Config) -> None:
    skills = cfg.hermes_home / "skills"
    for n in range(2):
        folder = skills / f"restore-{n}"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: restore-{n}\ndescription: Atlas restore runbook\n---\n"
            "# Atlas restore runbook\nTargets only the secondary Atlas server.\n")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    baseline = index.search_index(cfg.state_dir / "index.sqlite", QUERY, limit=2, artifact_type="skill")
    target = baseline[-1]["id"]
    endorse(cfg, target)
    q = queue(cfg)
    args = {"query": QUERY, "limit": 2, "artifact_type": "skill"}
    try:
        assert search(q, args=args)["results"][0]["id"] == target
        assert complete(cfg)["verified"] == 1
        receipt = json.loads(cases(cfg)[0]["result"])["sources"][0]
        artifact = index.get_artifact(cfg.state_dir / "index.sqlite", target)
        assert artifact is not None
        assert Path(receipt["locator"]) == (Path(artifact["path"]).expanduser() / "SKILL.md").resolve()
        assert [r["id"] for r in search(q, args=args)["results"]] == [r["id"] for r in baseline]
    finally:
        q.close(5)


@pytest.mark.parametrize("reason", ["unsupported", "unavailable"])
def test_unread_source_cannot_support_negative_verdict(cfg: Config, reason: str) -> None:
    if reason == "unsupported":
        cron = cfg.hermes_home / "cron"
        cron.mkdir()
        (cron / "jobs.json").write_text(json.dumps({"jobs": [{
            "id": f"restore-{n}", "name": f"Atlas restore runbook {n}",
            "prompt": "Restore only the secondary Atlas server.", "schedule_display": "daily",
        } for n in range(2)]}))
        index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
        baseline = index.search_index(cfg.state_dir / "index.sqlite", QUERY,
                                      artifact_type="cron_job", limit=2)
        assert len(baseline) == 2
        target = baseline[-1]["id"]
        endorse(cfg, target)
    else:
        _, _, target = prepare(cfg)
        artifact = index.get_artifact(cfg.state_dir / "index.sqlite", target)
        assert artifact is not None
        (cfg.source_root / artifact["path"]).unlink()

    class UnreadModel(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            packet = json.loads(kwargs["input"][0]["text"])
            assert not packet["sources"]
            return SimpleNamespace(parsed={"verdict": "inapplicable",
                                           "basis": "scope_target_incompatibility", "citations": []}, usage={})

    q = queue(cfg)
    try:
        args = {"query": QUERY, "limit": 2,
                "artifact_type": "cron_job" if reason == "unsupported" else "runbook"}
        assert search(q, args=args)["results"][0]["id"] == target
        model = UnreadModel()
        assert complete(cfg, model)["unresolved"] == 1
        assert len(model.calls) == 1
        assert search(q, args=args)["results"][0]["id"] == target
    finally:
        q.close(5)


@pytest.mark.parametrize("missing", ["task", "api", "lookup"])
def test_scope_absence_and_invalid_lookup_do_not_borrow_cached_task(cfg: Config, missing: str) -> None:
    _, _, target = prepare(cfg)
    q = queue(cfg)
    try:
        search(q)
        complete(cfg)
        ids = dict(IDS)
        args: dict[str, Any] = {"query": QUERY, "limit": 2, "artifact_type": "runbook"}
        if missing == "task":
            shadow_hooks.on_session_end(**IDS)
            args["user_request"] = REQUEST  # Handler arguments cannot confer host authority.
        elif missing == "api":
            ids.pop("api_request_id")
        else:
            args["lookup"] = {"unknown": "not a supported field"}
        assert search(q, args=args, ids=ids)["results"][0]["id"] == target
        assert len(cases(cfg)) == 1 and cases(cfg)[0]["status"] == "ai_verified"
    finally:
        q.close(5)


def test_promotion_receipts_are_not_shadow_replacement_routes(cfg: Config) -> None:
    prepare(cfg)
    q = queue(cfg)
    try:
        search(q)
        complete(cfg)
        row = cases(cfg)[0]
        assert shadow._reuse_candidates(cfg, {**row, "id": "different-case"}, shadow._task_packet(row)) == []
        # Even corrupt promotion results cannot poison shadow-mode route shortlisting after rollback.
        with shadow._connect(cfg, create=True) as conn:
            conn.execute("UPDATE cases SET result='not json' WHERE id=?", (row["id"],))
        assert shadow._reuse_candidates(cfg, {**row, "id": "different-case"}, shadow._task_packet(row)) == []
    finally:
        q.close(5)


def test_real_host_threaded_native_deferred_parallel_scope_and_reset(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, official_host_manager: Any,
) -> None:
    host = importlib.import_module("hermes_cli.plugins")
    middleware = importlib.import_module("hermes_cli.middleware")
    manager = official_host_manager
    manifest = host.PluginManifest(name="local_knowledge", key="local_knowledge", source="test")
    q: observer.Observer | None = None

    class Context(host.PluginContext):  # type: ignore[name-defined]
        def register_middleware(self, kind: str, callback: Any) -> Any:
            nonlocal q
            q = callback.__self__
            return super().register_middleware(kind, callback)

    monkeypatch.setattr(plugin, "_on_okf_post_tool_call", lambda **k: None)
    plugin.register(Context(manifest, manager))
    assert q is not None
    for n in range(2):
        manager.invoke_hook("pre_llm_call", **{**IDS, "turn_id": f"turn-{n}"},
                            user_message=f"Exact host request {n}")
    assert q.drain(5)
    barrier = threading.Barrier(2)

    def run(n: int) -> str:
        ids = {**IDS, "turn_id": f"turn-{n}", "tool_call_id": f"call-{n}"}
        context = copy_context()

        def deferred(args: dict[str, Any]) -> str:
            # Handler receives neither turn nor API IDs, exactly like deferred native dispatch.
            scope = shadow_hooks.active_scope(cfg)
            assert scope is not None
            barrier.wait(5)
            text = scope.request.text
            if n == 0:
                result = plugin._handle_search(args, session_id="session", task_id="task")
                assert json.loads(result)["success"] is True
            assert shadow_hooks.active_scope(cfg) is scope
            return text

        answer = context.run(middleware.run_tool_execution_middleware, tool_name="knowledge_search",
                             args={"query": QUERY}, next_call=deferred, **ids)
        assert context.run(shadow_hooks._SEARCH_SCOPE.get) is None
        return answer

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, n) for n in range(2)]
            assert [f.result(10) for f in futures] == ["Exact host request 0", "Exact host request 1"]
        assert q.drain(5)
        shadow_hooks.on_pre_llm_call(**IDS, user_message=REQUEST)

        def failed(args: Any) -> None:
            assert shadow_hooks.active_scope(cfg) is not None
            raise RuntimeError("downstream failure")

        with pytest.raises(RuntimeError, match="downstream failure"):
            q.middleware({}, failed, tool_name="knowledge_search", **IDS)
        assert shadow_hooks._SEARCH_SCOPE.get() is None
        shadow_hooks.on_session_end(**IDS)
        assert shadow_hooks.active_scope(cfg) is None
    finally:
        q.close(5)
