from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hermes_local_knowledge import cli, implicit, index, plugin, shadow, shadow_hooks
from hermes_local_knowledge.config import Config, VerifiedRoutingSettings, resolve_config

IDENTITY = {"session_id": "session", "task_id": "task", "turn_id": "turn"}
REQUEST = "  Locate the Atlas restore runbook for the primary server.\n"
QUERY = "Atlas restore runbook"
_REAL_SPAWN = shadow_hooks._spawn_worker


@pytest.fixture(autouse=True)
def isolated_requests(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    for key in ("LOCAL_KNOWLEDGE_ROOT", "LOCAL_KNOWLEDGE_STATE_DIR",
                shadow_hooks.SHADOW_WORKER_ENV, shadow_hooks.OKF_WORKER_ENV):
        monkeypatch.delenv(key, raising=False)
    # Unit hooks never launch an actual host/model; integration opts in below.
    monkeypatch.setattr(shadow_hooks, "_spawn_worker", lambda cfg: True)
    with shadow_hooks._LOCK:
        shadow_hooks._REQUESTS.clear()
    yield
    with shadow_hooks._LOCK:
        shadow_hooks._REQUESTS.clear()
    implicit.on_session_end()


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    home = tmp_path / "profile"
    home.mkdir()
    root = tmp_path / "source"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "restore.md").write_text(
        "# Atlas restore runbook\nRestore the primary Atlas server from a verified backup.\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        f"local_knowledge:\n  source_root: {root}\n  okf:\n    enabled: false\n"
        "  verified_routing:\n    mode: shadow\n", encoding="utf-8",
    )
    config = resolve_config(home)
    monkeypatch.setattr(plugin, "resolve_config", lambda: config)
    monkeypatch.setattr(shadow_hooks, "resolve_config", lambda: config)
    return config


def threaded(callback: Any, **kwargs: Any) -> Any:
    # Mirrors Hermes' copied-context worker, including no context propagation back.
    context = copy_context()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: context.run(callback, **kwargs)).result(timeout=5)


def bind(**kwargs: Any) -> None:
    threaded(plugin._bind_implicit_pre_llm_context,
             **{**IDENTITY, "user_message": REQUEST, **kwargs})


def search(**kwargs: Any) -> str:
    # Real host registry handlers do NOT receive the post hook's turn or API IDs.
    return plugin._handle_search({"query": QUERY}, **{
        "session_id": IDENTITY["session_id"], "task_id": IDENTITY["task_id"], **kwargs,
    })


def post(result: str, **kwargs: Any) -> None:
    threaded(shadow_hooks.on_post_tool_call, **{
        **IDENTITY, "api_request_id": "api", "tool_name": "knowledge_search",
        "args": {"query": QUERY}, "status": "success", "result": result, **kwargs,
    })


def test_explicit_lookup_capture_keeps_full_page_results_and_no_later_context(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for n in range(30):
        (cfg.source_root / "docs" / f"atlas-restore-{n}.md").write_text(
            f"# Atlas restore reference {n}\nAtlas restore evidence for the primary server.\n")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    calls: list[str] = []
    monkeypatch.setattr(shadow, "_call", lambda *a, **k: calls.append("inline inference"))
    bind(user_message="Continue", conversation_history=[{"role": "user", "content": "HISTORY CANARY"}])
    args = {"query": QUERY, "limit": 30}
    original = json.loads(plugin._handle_search(args, session_id="session", task_id="task"))
    lookup = {"intent": "Locate Atlas primary restore evidence", "target": "Atlas primary",
              "operation": "locate restore documents", "context": "Pre-search assistant retrieval target, not authorization."}
    enriched = plugin._handle_search({**args, "lookup": lookup}, session_id="session", task_id="task")
    payload = json.loads(enriched)
    assert {k: v for k, v in payload.items() if k != "usage_event_id"} == {
        k: v for k, v in original.items() if k != "usage_event_id"}
    assert not shadow._path(cfg).exists()
    post(enriched, args={**args, "lookup": lookup})
    bind(user_message="LATER USER CONTEXT", conversation_history=[{"role": "tool", "content": "TOOL CANARY"}])
    with shadow._connect(cfg) as conn:
        row = dict(conn.execute("SELECT * FROM cases").fetchone())
    assert row["user_request"] == "Continue"
    assert len(json.loads(row["baseline_ids"])) == 30
    assert json.loads(row["baseline_ids"]) == [item["id"] for item in payload["results"]]
    packet = json.loads(row["lookup_context"])
    assert packet["lookup"]["fields"] == lookup
    assert packet["lookup"]["provenance"] == "assistant_supplied_not_authority"
    assert not any(canary in str(row) for canary in ("HISTORY CANARY", "LATER USER CONTEXT", "TOOL CANARY"))
    assert calls == [] and row["calls"] == 0


def test_off_has_no_request_state_files_or_launch(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    off = replace(cfg, verified_routing=VerifiedRoutingSettings())
    monkeypatch.setattr(shadow_hooks, "resolve_config", lambda: off)
    calls: list[str] = []
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: calls.append("observe"))
    monkeypatch.setattr(shadow, "has_work", lambda *a: calls.append("check"))
    monkeypatch.setattr(shadow_hooks, "_spawn_worker", lambda *a: calls.append("spawn"))
    bind(conversation_history=[{"role": "user", "content": "history canary"}])
    post(json.dumps({"success": True, "usage_event_id": 1}))
    threaded(shadow_hooks.on_session_end, **IDENTITY)
    assert threaded(shadow_hooks.on_session_finalize, **IDENTITY) is False
    assert not shadow_hooks._REQUESTS
    assert not cfg.state_dir.exists()
    assert calls == []


def test_threaded_receipt_join_preserves_exact_request_and_baseline(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, Any]] = []
    monkeypatch.setattr(shadow, "observe", lambda config, **kwargs: observed.append(kwargs))
    bind(conversation_history=[{"role": "user", "content": "DO NOT CAPTURE HISTORY"}])
    result = search()
    payload = json.loads(result)
    assert payload["success"] and payload["results"]
    assert observed == []  # No direct-handler capture, even when opted in.
    post(result)
    post(result)  # At-least-once hook delivery must not become recurrence evidence.
    assert observed == [{
        **IDENTITY, "user_request": REQUEST, "query": QUERY, "artifact_type": "",
        "baseline_ids": [row["id"] for row in payload["results"]], "lookup": None,
    }]
    assert "DO NOT CAPTURE HISTORY" not in repr(observed)
    assert "shadow" not in result and "user_request" not in result


@pytest.mark.parametrize("changed", [
    {"session_id": "other"}, {"task_id": "other"}, {"turn_id": "other"},
    {"session_id": ""}, {"task_id": ""}, {"turn_id": ""}, {"api_request_id": ""},
    {"parent_session_id": "parent"}, {"worker_generated": True},
    {"tool_name": "tool_call"}, {"status": "error"},
    {"args": {"query": "different"}}, {"args": {"query": QUERY, "artifact_type": "doc"}},
])
def test_missing_or_cross_context_post_abstains(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, changed: dict[str, Any],
) -> None:
    observed: list[Any] = []
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: observed.append(k))
    bind()
    post(search(), **changed)
    assert observed == []


@pytest.mark.parametrize("user_message", [None, "", "  ", ["multimodal"], "x" * 4001])
def test_missing_nonstring_or_oversized_request_never_uses_history(
    cfg: Config, user_message: Any,
) -> None:
    bind(user_message=user_message, conversation_history=[{"role": "user", "content": REQUEST}])
    assert shadow_hooks._REQUESTS == {}


@pytest.mark.parametrize("field", ["session_id", "task_id", "turn_id", "api_request_id"])
def test_conflicting_receipt_attribution_abstains(
    cfg: Config, field: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Any] = []
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: observed.append(k))
    bind()
    result = search()
    with sqlite3.connect(cfg.state_dir / "usage.sqlite") as conn:
        conn.execute(f"UPDATE usage_events SET {field}='other'")
    post(result)
    assert observed == []


def test_receipt_uses_unassisted_baseline_not_visible_assisted_rows(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Any] = []
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: observed.append(k))
    bind()
    result = search()
    with sqlite3.connect(cfg.state_dir / "usage.sqlite") as conn:
        conn.execute("UPDATE usage_events SET baseline_top_ids_json=?", ('["doc:baseline"]',))
    post(result)
    assert observed[0]["baseline_ids"] == ["doc:baseline"]


def test_failed_search_and_shadow_failure_leave_response_unchanged(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[Any] = []
    bind()
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: calls.append(k))
    failed = plugin._handle_search({"query": ""}, **IDENTITY)
    post(failed)
    assert not calls
    result = search()
    before = json.loads(result)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("private exception canary")

    monkeypatch.setattr(shadow, "observe", fail)
    with caplog.at_level("DEBUG", logger=shadow_hooks.__name__):
        post(result)
    assert json.loads(result) == before
    assert "private exception canary" not in caplog.text


def test_profile_and_corpus_isolation_with_shared_state(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Any] = []
    monkeypatch.setattr(shadow, "observe", lambda config, **k: observed.append((config, k)))
    bind()
    result = search()
    other = replace(cfg, hermes_home=cfg.hermes_home.parent / "other")
    monkeypatch.setattr(shadow_hooks, "resolve_config", lambda: other)
    post(result)
    assert observed == []
    bind(user_message="Locate the Boreal restore runbook for the replica server.")
    threaded(shadow_hooks.on_session_end, **IDENTITY)
    monkeypatch.setattr(shadow_hooks, "resolve_config", lambda: cfg)
    post(result)
    assert observed[0][1]["user_request"] == REQUEST
    monkeypatch.setattr(shadow_hooks, "resolve_config", lambda: replace(cfg, source_root=cfg.source_root / "different"))
    post(result)
    assert len(observed) == 1


def test_context_bound_ttl_and_exact_turn_cleanup(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(shadow_hooks.time, "monotonic", lambda: clock[0])
    for number in range(shadow_hooks.MAX_CONTEXTS + 1):
        shadow_hooks.on_pre_llm_call(**{**IDENTITY, "turn_id": str(number), "user_message": REQUEST})
    assert len(shadow_hooks._REQUESTS) == shadow_hooks.MAX_CONTEXTS
    assert not any(key[-1] == "0" for key in shadow_hooks._REQUESTS)
    shadow_hooks.on_session_end(**{**IDENTITY, "turn_id": "1"})
    assert not any(key[-1] == "1" for key in shadow_hooks._REQUESTS)
    assert any(key[-1] == "2" for key in shadow_hooks._REQUESTS)
    clock[0] += shadow_hooks.CONTEXT_TTL_SECONDS
    shadow_hooks.on_pre_llm_call(**IDENTITY, user_message=None)
    assert not shadow_hooks._REQUESTS


@pytest.mark.parametrize("env", [shadow_hooks.SHADOW_WORKER_ENV, shadow_hooks.OKF_WORKER_ENV])
def test_worker_recursion_guard(cfg: Config, monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv(env, "1")
    bind()
    assert not shadow_hooks._REQUESTS
    assert shadow_hooks.on_session_finalize(**IDENTITY) is False
    assert not cfg.state_dir.exists()


def test_real_host_threaded_hooks_and_underlying_deferred_search_receipt(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = importlib.import_module("hermes_cli.plugins")
    manager = host.PluginManager()
    manifest = host.PluginManifest(name="local_knowledge", key="local_knowledge", source="test")
    observed: list[Any] = []
    hook_threads: list[threading.Thread] = []
    original_bind = shadow_hooks.on_pre_llm_call

    def record_thread(**kwargs: Any) -> None:
        hook_threads.append(threading.current_thread())
        original_bind(**kwargs)

    def observe(config: Config, **kwargs: Any) -> None:
        hook_threads.append(threading.current_thread())
        observed.append(kwargs)

    monkeypatch.setattr(shadow_hooks, "on_pre_llm_call", record_thread)
    monkeypatch.setattr(shadow, "observe", observe)
    monkeypatch.setattr(plugin, "_on_okf_post_tool_call", lambda **k: None)
    plugin.register(host.PluginContext(manifest, manager))
    try:
        # PyPI hosts invoke inline; newer hosts may add their own executor.
        # Dispatch in separate copied contexts so both exercise the real manager
        # across threads, without propagating the pre-hook context back to search.
        threaded(manager.invoke_hook, hook_name="pre_llm_call", **IDENTITY, user_message=REQUEST)
        result = search()  # Same handler seam as the unwrapped deferred dispatch.
        threaded(manager.invoke_hook, hook_name="post_tool_call", **IDENTITY,
                 api_request_id="api", tool_name="knowledge_search",
                 args={"query": QUERY}, status="success", result=result)
        assert observed[0]["user_request"] == REQUEST
        assert len(hook_threads) == 2
        # Retain Thread objects: operating systems may reuse exited thread IDs.
        assert hook_threads[0] is not hook_threads[1]
        assert all(thread is not threading.current_thread() for thread in hook_threads)
        implicit.on_pre_llm_call(**IDENTITY)
        # Existing implicit cleanup is retained in the composed hook.
        plugin._on_session_end(**IDENTITY)
        assert implicit._resolved_turn_id(session_id="session", task_id="task", turn_id=None) == ""
        assert not shadow_hooks._REQUESTS
    finally:
        unload = getattr(manager, "unload", None)
        if callable(unload):
            unload()


def test_lifecycle_detaches_real_fake_host_worker_and_passes_llm(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shadow_hooks, "_spawn_worker", _REAL_SPAWN)
    fake_host = cfg.hermes_home / "hermes_cli"
    fake_host.mkdir()
    (fake_host / "__init__.py").write_text("", encoding="utf-8")
    (fake_host / "main.py").write_text(
        "import argparse\nfrom types import SimpleNamespace\n"
        "from hermes_local_knowledge.cli import setup_hermes_cli, handle_hermes_cli\n"
        "class Llm:\n"
        " def complete_structured(self, **kwargs):\n"
        "  return SimpleNamespace(parsed={'action': 'unresolved'}, usage={'total_tokens': 7})\n"
        "parser=argparse.ArgumentParser()\n"
        "sub=parser.add_subparsers(dest='command', required=True)\n"
        "setup_hermes_cli(sub.add_parser('local-knowledge'))\n"
        "raise SystemExit(handle_hermes_cli(parser.parse_args(), llm=Llm()))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(cfg.hermes_home), str(Path(__file__).resolve().parents[1]))))
    launched: list[subprocess.Popen[Any]] = []
    start_reaper = shadow_hooks._start_worker_reaper

    def record_process(process: subprocess.Popen[Any]) -> None:
        launched.append(process)
        start_reaper(process)

    monkeypatch.setattr(shadow_hooks, "_start_worker_reaper", record_process)
    monkeypatch.setattr(plugin, "_on_okf_session_finalize", lambda **k: False)
    bind()
    post(search())
    assert not shadow.has_work(cfg)
    assert plugin._on_session_finalize(**IDENTITY) is False
    plugin._on_session_end(**IDENTITY)
    assert len(launched) == 1  # Turn-end alone launches; no teardown/new message.
    # Only the test waits. The real finalizer returned immediately after launch.
    assert launched[0].wait(timeout=15) == 0  # Supervisor reached idle, even with unresolved results.
    report = shadow.report(cfg)
    assert report["cases"] == {"unresolved": 1}
    assert report["model_calls"] == 1
    assert report["usage"]["total_tokens"] == 7
    assert not shadow.has_work(cfg)
    assert (cfg.state_dir / "routing_worker.log").read_text() == "Worker launched.\n"


def test_spawn_command_is_shell_free_profile_bound_and_recursion_safe(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def popen(command: list[str], **kwargs: Any) -> Any:
        calls.append((command, kwargs))
        return object()

    monkeypatch.setattr(shadow_hooks.subprocess, "Popen", popen)
    monkeypatch.setattr(shadow_hooks, "_start_worker_reaper", lambda process: None)
    assert _REAL_SPAWN(cfg)
    command, kwargs = calls[0]
    assert command[:-2] == [sys.executable, "-m", "hermes_cli.main", "local-knowledge", "routing-supervisor",
                           "--hermes-home", str(cfg.hermes_home)]
    assert command[-2] == "--wake-ns"
    assert int(command[-1]) > 0
    assert kwargs.get("shell", False) is False
    assert kwargs["cwd"] == str(cfg.hermes_home)
    assert kwargs["env"]["HERMES_HOME"] == str(cfg.hermes_home)
    assert kwargs["env"][shadow_hooks.SHADOW_WORKER_ENV] == "1"
    assert kwargs["env"][shadow_hooks.OKF_WORKER_ENV] == "1"
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    if os.name == "nt":
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP") | getattr(subprocess, "DETACHED_PROCESS")
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True
        assert "creationflags" not in kwargs


def test_cli_report_and_native_worker_bridge(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    parser = argparse.ArgumentParser()
    cli.setup_hermes_cli(parser)
    llm = object()
    calls: list[Any] = []

    def worker(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(shadow, "run_worker", worker)
    args = parser.parse_args(["routing-worker", "--hermes-home", str(cfg.hermes_home)])
    assert cli.handle_hermes_cli(args, llm=llm) == 0
    assert calls == [{"llm": llm, "hermes_home": cfg.hermes_home}]
    monkeypatch.setattr(shadow, "run_worker", lambda **kwargs: 1)
    with pytest.raises(SystemExit) as exc:
        cli.handle_hermes_cli(args, llm=llm)
    assert exc.value.code == 1
    assert cli.main(["routing-report", "--hermes-home", str(cfg.hermes_home), "--json"]) == 0
    standalone = json.loads(capsys.readouterr().out)
    args = parser.parse_args(["routing-report", "--hermes-home", str(cfg.hermes_home), "--json"])
    assert cli.handle_hermes_cli(args) == 0
    assert json.loads(capsys.readouterr().out) == standalone == shadow.report(cfg)
    assert not cfg.state_dir.exists()
    with pytest.raises(SystemExit) as exc:
        cli.main(["routing-worker"])
    assert exc.value.code == 2  # Standalone intentionally has no host-LLM worker.


def test_disabling_clears_profile_text_even_after_root_changes(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind()
    assert shadow_hooks._REQUESTS
    off = replace(cfg, source_root=cfg.source_root / "changed", verified_routing=VerifiedRoutingSettings())
    monkeypatch.setattr(shadow_hooks, "resolve_config", lambda: off)
    bind(turn_id="next-turn")
    assert not shadow_hooks._REQUESTS


def test_opt_in_does_not_change_native_ranking_or_response_shape(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    off = replace(cfg, verified_routing=VerifiedRoutingSettings())
    monkeypatch.setattr(plugin, "resolve_config", lambda: off)
    assert json.loads(search())["success"]  # Warm the existing lazy index build in both modes.
    before = json.loads(search())
    monkeypatch.setattr(plugin, "resolve_config", lambda: cfg)
    bind()
    result = search()
    post(result)
    after = json.loads(result)
    assert before["results"] == after["results"]
    assert before.keys() == after.keys()
    before.pop("usage_event_id")
    after.pop("usage_event_id")
    assert before == after
    assert shadow.report(cfg)["counters"]["captures"] == 1


def test_spawn_failure_keeps_ready_work_for_recovery(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind()
    post(search())
    shadow_hooks.on_session_end(**IDENTITY)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("spawn failed")

    monkeypatch.setattr(shadow_hooks, "_spawn_worker", _REAL_SPAWN)
    monkeypatch.setattr(shadow_hooks.subprocess, "Popen", fail)
    assert shadow_hooks.on_session_finalize(**IDENTITY) is False
    assert shadow.has_work(cfg)
    assert not (cfg.state_dir / "routing_worker.log").exists()


def test_finalizer_composition_does_not_short_circuit_workers(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def okf_finalize(**kwargs: Any) -> bool:
        calls.append("okf")
        return True

    def shadow_finalize(**kwargs: Any) -> bool:
        calls.append("shadow")
        return True

    monkeypatch.setattr(plugin, "_on_okf_session_finalize", okf_finalize)
    monkeypatch.setattr(shadow_hooks, "on_session_finalize", shadow_finalize)
    assert plugin._on_session_finalize(**IDENTITY)
    assert calls == ["okf", "shadow"]
