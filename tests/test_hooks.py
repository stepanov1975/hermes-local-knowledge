from __future__ import annotations

import json
import sqlite3
import sys
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
    monkeypatch.delenv(okf.OKF_WORKER_ENV, raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
  okf:
    enabled: {str(enabled).lower()}
    auto_generate: {str(auto_generate).lower()}
    max_candidates_per_session: 2
    max_worker_seconds: 120
    min_use_count: 1
    worker_toolsets: terminal,file
    worker_source: okf-worker-test
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

    plugin.register(Ctx())

    assert [call["name"] for call in tool_calls] == [
        "knowledge_search",
        "knowledge_get",
        "knowledge_neighbors",
        "knowledge_feedback",
        "knowledge_usage_report",
    ]
    assert skill_calls[0][0] == "local-knowledge-router"
    assert hook_calls == [
        ("post_tool_call", plugin._on_post_tool_call),
        ("on_session_end", plugin._on_session_end),
    ]


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


def test_post_tool_call_skips_inside_okf_worker(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setenv(okf.OKF_WORKER_ENV, "1")

    hooks._on_post_tool_call(tool_name="knowledge_search", args={"query": "paperless"}, result="{}")

    assert not okf.okf_queue_db_path(state_dir).exists()


def test_session_end_does_not_spawn_when_auto_generate_false(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=False)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("spawned")))

    assert hooks._on_session_end(session_id="s", completed=True, interrupted=False, model="m", platform="cli") is False


def test_session_end_spawns_once_with_lock_and_env_guard(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )
    calls: list[dict[str, Any]] = []

    class FakePopen:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            calls.append({"command": command, **kwargs})

    monkeypatch.setattr(hooks.subprocess, "Popen", FakePopen)

    assert hooks._on_session_end(session_id="s", completed=True, interrupted=False, model="m", platform="cli") is True
    assert len(calls) == 1
    call = calls[0]
    assert call["command"][:3] == [sys.executable, "-m", "hermes_local_knowledge.okf_worker"]
    assert "--timeout" in call["command"]
    assert "120" in call["command"]
    assert "--toolsets" in call["command"]
    assert "terminal,file" in call["command"]
    assert "--lock-path" in call["command"]
    assert str(okf.worker_lock_path(state_dir)) in call["command"]
    assert call["env"][okf.OKF_WORKER_ENV] == "1"
    assert call["env"]["HERMES_HOME"] == str(hermes_home.resolve())
    assert str(Path(hooks.__file__).resolve().parents[1]) in call["env"]["PYTHONPATH"]
    assert call["start_new_session"] is True
    assert okf.worker_lock_path(state_dir).exists()

    # Existing fresh lock suppresses a second spawn in the same session-end window.
    assert hooks._on_session_end(session_id="s", completed=True, interrupted=False, model="m", platform="cli") is False
    assert len(calls) == 1


def test_session_end_skips_when_worker_lock_exists_or_no_pending_candidates(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("spawned")))

    assert hooks._on_session_end(session_id="s", completed=True, interrupted=False, model="m", platform="cli") is False

    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )
    lock_path = okf.worker_lock_path(state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 123, "created_at": time.time()}), encoding="utf-8")

    assert hooks._on_session_end(session_id="s", completed=True, interrupted=False, model="m", platform="cli") is False
