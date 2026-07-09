from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
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
    host_llm = object()

    class Ctx:
        llm = host_llm

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
    assert hook_calls[1][0] == "on_session_finalize"
    assert callable(hook_calls[1][1])
    assert hook_calls[1][1].keywords["llm"] is host_llm


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


def test_session_finalize_does_not_generate_when_auto_generate_false(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=False)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )
    class FailLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("generated")

    assert hooks._on_session_finalize(llm=FailLlm(), session_id="s", platform="cli") is False


def test_session_finalize_generates_bounded_okf_with_host_llm(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema=schema,
        args={"query": "private customer text"},
    )
    calls: list[dict[str, Any]] = []

    class FakeLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            return SimpleNamespace(
                parsed={
                    "okfs": [
                        {
                            "tool": "knowledge_search",
                            "schema_hash": okf.schema_hash(schema),
                            "title": "Local knowledge search",
                            "aliases": ["search local operational knowledge"],
                            "triggers": ["find the right local artifact"],
                            "when_not_to_use": ["when the user already supplied the exact artifact"],
                            "related_tools": ["knowledge_get"],
                            "body": "Use this tool to route a local question to the most relevant whole artifact.",
                        }
                    ]
                }
            )

    assert hooks._on_session_finalize(llm=FakeLlm(), session_id="s", platform="cli") is True
    assert len(calls) == 1
    call = calls[0]
    assert call["timeout"] == 120
    assert call["purpose"] == "local_knowledge.okf_generation"
    assert "private customer text" not in json.dumps(call)
    assert okf.queue_counts(state_dir) == {"done": 1}
    output = okf.okf_file_path(state_dir, "knowledge_search")
    assert output.exists()
    assert "artifact_type: tool_okf" in output.read_text(encoding="utf-8")
    assert not okf.generation_lock_path(state_dir).exists()


def test_session_finalize_releases_claims_when_host_llm_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )

    class FailLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            raise TimeoutError("model timeout")

    assert hooks._on_session_finalize(llm=FailLlm(), session_id="s", platform="cli") is False
    assert okf.queue_counts(state_dir) == {"pending": 1}
    assert not okf.generation_lock_path(state_dir).exists()


def test_session_finalize_uses_one_call_and_honors_candidate_limit(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    schema = {"type": "object"}
    for tool_name in ("alpha_tool", "beta_tool", "gamma_tool"):
        okf.upsert_tool_candidate(
            state_dir,
            tool_name=tool_name,
            toolset="demo",
            schema=schema,
            args={},
        )
    calls: list[dict[str, Any]] = []

    class FakeLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            packets = json.loads(kwargs["input"][0]["text"])["candidates"]
            assert len(packets) == 2
            return SimpleNamespace(
                parsed={
                    "okfs": [
                        {
                            "tool": packet["tool"],
                            "schema_hash": packet["schema_hash"],
                            "title": f"Tool OKF: {packet['tool']}",
                            "aliases": [f"route requests through {packet['tool']}"],
                            "triggers": [f"use {packet['tool']} for this operation"],
                            "when_not_to_use": ["when a different exact tool is requested"],
                            "related_tools": [],
                            "body": f"Use {packet['tool']} for the matching operation.",
                        }
                        for packet in packets
                    ]
                }
            )

    assert hooks._on_session_finalize(llm=FakeLlm(), session_id="s", platform="cli") is True
    assert len(calls) == 1
    assert okf.queue_counts(state_dir) == {"done": 2, "pending": 1}


def test_session_finalize_recovers_stale_claim_before_generation(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, _hermes_home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    schema = {"type": "object"}
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema=schema,
        args={},
    )
    claimed = okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="abandoned",
        now="2000-01-01T00:00:00Z",
    )
    assert len(claimed) == 1

    class FakeLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(
                parsed={
                    "okfs": [
                        {
                            "tool": packet["tool"],
                            "schema_hash": packet["schema_hash"],
                            "title": "Local knowledge search",
                            "aliases": ["search local operational knowledge"],
                            "triggers": ["find the right local artifact"],
                            "when_not_to_use": ["when the exact artifact is already known"],
                            "related_tools": [],
                            "body": "Route a local question to the most relevant whole artifact.",
                        }
                    ]
                }
            )

    assert hooks._on_session_finalize(llm=FakeLlm(), session_id="s", platform="cli") is True
    assert okf.queue_counts(state_dir) == {"done": 1}
