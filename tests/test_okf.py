from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermes_local_knowledge import okf, plugin


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def db_text(state_dir: Path) -> str:
    with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
        rows = conn.execute("SELECT * FROM okf_candidates").fetchall()
    return repr(rows)


def test_safe_arg_shape_does_not_persist_values(tmp_path: Path) -> None:
    args = {
        "query": "find alice private tax document token=abc123",
        "metadata": {
            "api_key": "sk-secret-value",
            "limit": 5,
            "paths": ["/home/alex/private.pdf", "/tmp/other.pdf"],
        },
    }

    shape = okf.safe_arg_shape(args)
    rendered = json.dumps(shape, sort_keys=True)

    assert "field_0" in rendered
    assert "field_1" in rendered
    assert "str" in rendered
    assert "int" in rendered
    assert "query" not in rendered
    assert "metadata" not in rendered
    assert "api_key" not in rendered
    assert "find alice" not in rendered
    assert "abc123" not in rendered
    assert "sk-secret-value" not in rendered
    assert "/home/alex/private.pdf" not in rendered

    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="paperless_find_latest_document",
        toolset="paperless",
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
        args=args,
    )

    persisted = db_text(tmp_path)
    assert "find alice" not in persisted
    assert "abc123" not in persisted
    assert "sk-secret-value" not in persisted
    assert "/home/alex/private.pdf" not in persisted


def test_schema_view_redacts_defaults_examples_and_secret_like_descriptions(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "description": "Search customer OCR document text about divorce settlement and medical diagnosis for alice@example.com using token=abc123",
        "properties": {
            "query": {
                "type": "string",
                "default": "alice@example.com",
                "examples": ["sk-secret-value"],
            }
        },
    }

    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="paperless_find_latest_document",
        toolset="paperless",
        schema=schema,
        args={},
    )
    rows = okf.pending_candidates(tmp_path, limit=1)
    packet = okf.candidate_packet(rows[0], tmp_path)
    rendered = json.dumps(packet, sort_keys=True)
    persisted = db_text(tmp_path)

    assert packet["schema_hash"] == okf.schema_hash(schema)
    for value in ["alice@example.com", "token=abc123", "sk-secret-value", "divorce settlement"]:
        assert value not in rendered
        assert value not in persisted


def test_legacy_raw_schema_json_is_sanitized_on_read_and_migration(tmp_path: Path) -> None:
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="paperless_find_latest_document",
        toolset="paperless",
        schema={"type": "object"},
        args={},
    )
    legacy_schema = {
        "type": "object",
        "description": "Search customer OCR document text about divorce settlement and medical diagnosis for alice@example.com using token=abc123",
        "properties": {"query": {"type": "string", "default": "sk-secret-value"}},
    }
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET schema_json = ? WHERE tool_name = ?",
            (json.dumps(legacy_schema), "paperless_find_latest_document"),
        )

    rows = okf.pending_candidates(tmp_path, limit=1)
    packet = okf.candidate_packet(rows[0], tmp_path)
    rendered = json.dumps(packet, sort_keys=True)
    persisted = db_text(tmp_path)

    for value in ["alice@example.com", "token=abc123", "sk-secret-value", "divorce settlement"]:
        assert value not in rendered
        assert value not in persisted


def test_upsert_candidate_counts_success_and_error(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema=schema,
        args={"query": "paperless"},
        success=True,
        now="2026-07-09T18:00:00Z",
    )
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema=schema,
        args={"query": "paperless"},
        success=False,
        error_type="RuntimeError",
        error_message="api_key=secret should redact",
        now="2026-07-09T18:01:00Z",
    )

    rows = okf.pending_candidates(tmp_path, limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["use_count"] == 2
    assert row["success_count"] == 1
    assert row["error_count"] == 1
    assert row["last_error_type"] == "RuntimeError"
    assert row["last_error_message"] == "<redacted>"
    assert "secret" not in db_text(tmp_path)


def test_schema_migration_adds_missing_columns_without_invalid_constraints(tmp_path: Path) -> None:
    db_path = okf.okf_queue_db_path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE okf_candidates (
              tool_name TEXT PRIMARY KEY,
              first_seen TEXT,
              last_seen TEXT,
              use_count INTEGER DEFAULT 0,
              status TEXT DEFAULT 'pending'
            )
            """
        )
        conn.execute(
            "INSERT INTO okf_candidates(tool_name, first_seen, last_seen, use_count, status) VALUES (?, ?, ?, ?, ?)",
            ("legacy_tool", "2026-07-09T18:00:00Z", "2026-07-09T18:00:00Z", 1, "pending"),
        )

    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(okf_candidates)")}
    assert "claim_token" in columns
    assert "arg_shape_json" in columns


def test_schema_hash_change_requeues_done_candidate(tmp_path: Path) -> None:
    old_schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    new_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
    }
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema=old_schema,
        args={"query": "paperless"},
    )
    claimed = okf.claim_candidates(tmp_path, limit=1, claim_token="claim-1")
    assert len(claimed) == 1
    assert okf.mark_candidate_done(
        tmp_path,
        tool_name="knowledge_search",
        claim_token="claim-1",
        okf_path=tmp_path / "okfs" / "tools" / "knowledge-search.md",
    )

    assert okf.queue_counts(tmp_path) == {"done": 1}

    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema=new_schema,
        args={"query": "paperless", "limit": 5},
    )

    rows = okf.pending_candidates(tmp_path, limit=5)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["okf_path"] is None
    assert rows[0]["schema_hash"] == okf.schema_hash(new_schema)


def test_okf_config_reads_nested_config(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    state_dir = tmp_path / "state"
    repo.mkdir()
    hermes_home.mkdir()
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
  okf:
    enabled: false
    auto_generate: true
    max_candidates_per_session: 4
    max_worker_seconds: 240
    min_use_count: 3
    worker_toolsets: terminal,file
    worker_source: okf-worker-test
""",
    )

    cfg = plugin._runtime_config()

    assert cfg.okf.enabled is False
    assert cfg.okf.auto_generate is True
    assert cfg.okf.max_candidates_per_session == 4
    assert cfg.okf.max_worker_seconds == 240
    assert cfg.okf.min_use_count == 3
    assert cfg.okf.worker_toolsets == ("terminal", "file")
    assert cfg.okf.worker_source == "okf-worker-test"
