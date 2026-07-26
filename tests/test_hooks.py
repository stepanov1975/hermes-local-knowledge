from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from hermes_local_knowledge import hooks, okf, plugin


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def configure(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
    *,
    enabled: bool = True,
    auto_generate: bool = False,
) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    state_dir = tmp_path / "state"
    repo.mkdir()
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
  okf:
    enabled: {str(enabled).lower()}
    auto_generate: {str(auto_generate).lower()}
    max_candidates_per_session: 2
    max_generation_seconds: 120
    min_use_count: 1
""",
    )
    return repo, hermes_home, state_dir


def db_text(state_dir: Path) -> str:
    with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
        rows = conn.execute("SELECT * FROM okf_candidates").fetchall()
    return repr(rows)


def test_register_adds_okf_hooks() -> None:
    tool_calls: list[dict[str, Any]] = []
    skill_calls: list[tuple[str, Path]] = []
    hook_calls: list[tuple[str, Any]] = []
    class Ctx:
        def register_tool(self, **kwargs):  # type: ignore[no-untyped-def]
            tool_calls.append(kwargs)

        def register_skill(self, name, skill_md):  # type: ignore[no-untyped-def]
            skill_calls.append((name, Path(skill_md)))

        def register_hook(self, name, callback):  # type: ignore[no-untyped-def]
            hook_calls.append((name, callback))

    ctx = Ctx()
    plugin.register(ctx)

    assert [call["name"] for call in tool_calls] == [
        "knowledge_search",
        "knowledge_get",
        "knowledge_neighbors",
        "knowledge_feedback",
        "knowledge_usage_report",
    ]
    assert skill_calls[0][0] == "local-knowledge-router"
    assert hook_calls[0] == ("post_tool_call", plugin._on_post_tool_call)
    assert hook_calls[1] == ("on_session_finalize", plugin._on_session_finalize)


def test_post_tool_call_records_candidate_without_result_or_arg_values(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setattr(
        hooks,
        "_tool_metadata",
        lambda tool_name: ("paperless", {"type": "object", "properties": {"query": {"type": "string"}}}),
    )

    hooks._on_post_tool_call(
        tool_name="paperless_find_latest_document",
        args={"query": "alice private tax document", "api_key": "sk-secret"},
        result=json.dumps({"success": False, "error": "token=abc123 alice private tax document"}),
        task_id="session-1",
        duration_ms=17,
    )

    rows = okf.pending_candidates(state_dir, limit=5)
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "paperless_find_latest_document"
    assert rows[0]["toolset"] == "paperless"
    assert rows[0]["error_count"] == 1
    persisted = db_text(state_dir)
    assert "alice private" not in persisted
    assert "sk-secret" not in persisted
    assert "abc123" not in persisted


def test_post_tool_call_treats_null_error_as_success(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setattr(
        hooks,
        "_tool_metadata",
        lambda tool_name: ("terminal", {"type": "object"}),
    )

    hooks._on_post_tool_call(
        tool_name="terminal",
        args={"command": "true"},
        result=json.dumps({"output": "", "exit_code": 0, "error": None}),
    )

    rows = okf.pending_candidates(state_dir, limit=1)
    assert len(rows) == 1
    assert rows[0]["success_count"] == 1
    assert rows[0]["error_count"] == 0
    assert rows[0]["last_error_type"] is None


def test_result_classification_preserves_independent_failure_signals() -> None:
    assert hooks._classify_result(json.dumps({"success": False, "error": None})) == (
        False,
        "tool_error",
        "tool_error",
    )
    assert hooks._classify_result(json.dumps({"error": "command failed"})) == (
        False,
        "tool_error",
        "command failed",
    )


def test_success_status_overrides_failing_fallback_payload() -> None:
    assert hooks._classify_hook_outcome(
        {
            "status": "success",
            "result": json.dumps({"success": False, "error": "stale fallback"}),
        }
    ) == (True, None, None)


def test_post_tool_call_prefers_hermes_status_fields(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setattr(
        hooks,
        "_tool_metadata",
        lambda tool_name: ("terminal", {"type": "object"}),
    )

    hooks._on_post_tool_call(
        tool_name="terminal",
        args={"command": "long task"},
        result="Error executing tool 'terminal': timed out after 1.0s",
        status="timeout",
        error_type="tool_timeout",
        error_message="Error executing tool 'terminal': timed out after 1.0s",
    )

    rows = okf.pending_candidates(state_dir, limit=1)
    assert len(rows) == 1
    assert rows[0]["success_count"] == 0
    assert rows[0]["error_count"] == 1
    assert rows[0]["last_error_type"] == "tool_timeout"


def test_session_finalize_does_not_launch_when_auto_generate_false(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=False)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )
    monkeypatch.setattr(hooks, "_spawn_worker", lambda _cfg: (_ for _ in ()).throw(AssertionError("spawned")))

    assert hooks._on_session_finalize(session_id="s", platform="cli") is False
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_session_finalize_launches_worker_without_claiming_or_generating(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )
    launched = []

    def fake_spawn(cfg):  # type: ignore[no-untyped-def]
        launched.append(cfg)
        return True

    monkeypatch.setattr(hooks, "_spawn_worker", fake_spawn)

    class FailLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("the finalization hook called the model inline")

    assert hooks._on_session_finalize(llm=FailLlm(), session_id="s", platform="cli") is True
    assert len(launched) == 1
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_session_finalize_skips_worker_recursion(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    monkeypatch.setenv(hooks.OKF_WORKER_ENV, "1")
    monkeypatch.setattr(hooks, "_spawn_worker", lambda _cfg: (_ for _ in ()).throw(AssertionError("spawned")))

    assert hooks._on_session_finalize(session_id="worker", platform="cli") is False
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_post_tool_call_skips_worker_recursion(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setenv(hooks.OKF_WORKER_ENV, "true")

    hooks._on_post_tool_call(tool_name="terminal", args={"command": "ignored"}, result="ok")

    assert not okf.okf_queue_db_path(state_dir).exists()


def test_spawn_worker_uses_detached_plugin_cli_process(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    cfg = plugin._runtime_config()
    calls: list[tuple[list[str], dict[str, Any]]] = []
    reaped = []

    class FakeProcess:
        pid = 1234

        def wait(self):  # type: ignore[no-untyped-def]
            return 0

    def fake_popen(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(hooks.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(hooks, "_start_worker_reaper", lambda process: reaped.append(process), raising=False)

    assert hooks._spawn_worker(cfg) is True
    assert len(calls) == 1
    assert len(reaped) == 1
    command, kwargs = calls[0]
    assert command == [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "local-knowledge",
        "okf-worker",
        "--hermes-home",
        str(hermes_home.resolve()),
    ]
    assert kwargs["cwd"] == str(hermes_home.resolve())
    assert kwargs["env"][hooks.OKF_WORKER_ENV] == "1"
    assert kwargs["env"]["HERMES_HOME"] == str(hermes_home.resolve())
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert Path(kwargs["stdout"].name) == state_dir / "okf_worker.log"
    assert kwargs["stdout"].closed is True
    if os.name == "nt":
        expected_flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP"))
        expected_flags |= int(getattr(subprocess, "DETACHED_PROCESS"))
        assert kwargs["creationflags"] & expected_flags == expected_flags
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True
        assert "creationflags" not in kwargs
    assert (state_dir / "okf_worker.log").exists()


def test_worker_reaper_waits_for_detached_child() -> None:
    waited = threading.Event()

    class FakeProcess:
        pid = 4321

        def wait(self):  # type: ignore[no-untyped-def]
            waited.set()
            return 0

    hooks._start_worker_reaper(FakeProcess())  # type: ignore[arg-type]

    assert waited.wait(timeout=1)


def test_detached_process_kwargs_use_windows_detachment_flags(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(hooks.os, "name", "nt")
    monkeypatch.setattr(hooks.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(hooks.subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)

    kwargs = hooks._detached_process_kwargs()

    assert kwargs == {"creationflags": 0x00000208}


def test_session_finalize_keeps_candidate_pending_when_spawn_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    monkeypatch.setattr(hooks, "_spawn_worker", lambda _cfg: False)

    assert hooks._on_session_finalize(session_id="s", platform="telegram") is False
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_session_finalize_launches_worker_for_stale_claim_without_pending_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    assert okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="abandoned",
        now="2000-01-01T00:00:00Z",
    )
    launched = []

    def fake_spawn(cfg):  # type: ignore[no-untyped-def]
        launched.append(cfg)
        return True

    monkeypatch.setattr(hooks, "_spawn_worker", fake_spawn)

    assert hooks._on_session_finalize(session_id="next", platform="telegram") is True
    assert len(launched) == 1
    assert okf.queue_counts(state_dir) == {"claimed": 1}


def test_session_finalize_does_not_launch_for_fresh_claim_without_pending_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    assert okf.claim_candidates(state_dir, limit=1, claim_token="active")
    monkeypatch.setattr(hooks, "_spawn_worker", lambda _cfg: (_ for _ in ()).throw(AssertionError("spawned")))

    assert hooks._on_session_finalize(session_id="parallel", platform="telegram") is False
    assert okf.queue_counts(state_dir) == {"claimed": 1}


def test_session_finalize_returns_quickly_while_publication_holds_database_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    launched = []

    def fake_spawn(cfg):  # type: ignore[no-untyped-def]
        launched.append(cfg)
        return True

    monkeypatch.setattr(hooks, "_spawn_worker", fake_spawn)
    conn = sqlite3.connect(okf.okf_queue_db_path(state_dir))
    conn.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        assert hooks._on_session_finalize(session_id="locked", platform="telegram") is False
        elapsed = time.monotonic() - started
    finally:
        conn.rollback()
        conn.close()

    assert elapsed < 0.5
    assert launched == []
