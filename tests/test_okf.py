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


def test_canonical_arg_shape_migration_is_idempotent(tmp_path: Path) -> None:
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless", "limit": 5},
    )

    def persisted_shape() -> object:
        with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
            row = conn.execute(
                "SELECT arg_shape_json FROM okf_candidates WHERE tool_name = ?",
                ("knowledge_search",),
            ).fetchone()
        assert row is not None
        return json.loads(row[0])

    original = persisted_shape()
    okf.queue_counts(tmp_path)
    after_first_read = persisted_shape()
    okf.queue_counts(tmp_path)
    after_second_read = persisted_shape()

    assert after_first_read == original
    assert after_second_read == original


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


def test_legacy_raw_schema_and_arg_json_are_sanitized_on_read_and_migration(tmp_path: Path) -> None:
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
        "properties": {"query": {"type": "string", "default": "«redacted:sk-…»"}},
    }
    legacy_args = {
        "query": "alice private tax document",
        "path": "/home/alex/private.pdf",
        "api_key": "sk-secret",
    }
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET schema_json = ?, arg_shape_json = ? WHERE tool_name = ?",
            (json.dumps(legacy_schema), json.dumps(legacy_args), "paperless_find_latest_document"),
        )

    rows = okf.pending_candidates(tmp_path, limit=1)
    packet = okf.candidate_packet(rows[0], tmp_path)
    rendered = json.dumps(packet, sort_keys=True)
    persisted = db_text(tmp_path)

    for value in [
        "alice@example.com",
        "token=abc123",
        "«redacted:sk-…»",
        "divorce settlement",
        "alice private tax document",
        "/home/alex/private.pdf",
        "api_key",
        "sk-secret",
    ]:
        assert value not in rendered
        assert value not in persisted


def test_legacy_type_only_argument_is_not_mistaken_for_canonical_shape(tmp_path: Path) -> None:
    private_value = "private-medical-record-123"
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET arg_shape_json = ? WHERE tool_name = ?",
            (json.dumps({"type": private_value}), "knowledge_search"),
        )

    rows = okf.pending_candidates(tmp_path, limit=1)
    packet = okf.candidate_packet(rows[0], tmp_path)
    persisted = db_text(tmp_path)

    assert private_value not in json.dumps(packet, sort_keys=True)
    assert private_value not in persisted
    assert packet["arg_shape"]["type"] == "object"


def test_legacy_object_shape_with_private_truncated_value_is_resanitized(tmp_path: Path) -> None:
    private_value = "private-medical-record-object-456"
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    legacy = {
        "type": "object",
        "field_count": 0,
        "fields": {},
        "truncated": private_value,
    }
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET arg_shape_json = ? WHERE tool_name = ?",
            (json.dumps(legacy), "knowledge_search"),
        )

    rows = okf.pending_candidates(tmp_path, limit=1)
    packet = okf.candidate_packet(rows[0], tmp_path)

    assert private_value not in json.dumps(packet, sort_keys=True)
    assert private_value not in db_text(tmp_path)


def test_legacy_array_shape_with_private_truncated_value_is_resanitized(tmp_path: Path) -> None:
    private_value = "private-medical-record-array-789"
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    legacy = {
        "type": "array",
        "length": 0,
        "items": [],
        "truncated": private_value,
    }
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET arg_shape_json = ? WHERE tool_name = ?",
            (json.dumps(legacy), "knowledge_search"),
        )

    rows = okf.pending_candidates(tmp_path, limit=1)
    packet = okf.candidate_packet(rows[0], tmp_path)

    assert private_value not in json.dumps(packet, sort_keys=True)
    assert private_value not in db_text(tmp_path)


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
    assert rows[0]["attempt_count"] == 0


def test_mark_candidate_done_marks_index_dirty(tmp_path: Path) -> None:
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
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

    assert len(okf.index_dirty_tokens(tmp_path)) == 1


def test_recover_stale_claim_returns_retryable_candidate_to_pending(tmp_path: Path) -> None:
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )
    claimed = okf.claim_candidates(
        tmp_path,
        limit=1,
        claim_token="stale-claim",
        now="2026-07-09T18:00:00Z",
    )
    assert len(claimed) == 1

    recovered = okf.recover_stale_claims(
        tmp_path,
        stale_after_seconds=60,
        max_attempts=3,
        now="2026-07-09T18:02:00Z",
    )

    assert recovered == 1
    rows = okf.pending_candidates(tmp_path, limit=1)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["claim_token"] is None
    assert rows[0]["claimed_at"] is None


def test_recover_stale_claim_stops_after_attempt_limit(tmp_path: Path) -> None:
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={"query": "paperless"},
    )
    for attempt in range(3):
        token = f"claim-{attempt}"
        claimed = okf.claim_candidates(
            tmp_path,
            limit=1,
            claim_token=token,
            now=f"2026-07-09T18:0{attempt}:00Z",
        )
        assert len(claimed) == 1
        if attempt < 2:
            assert okf.mark_candidate_error(
                tmp_path,
                tool_name="knowledge_search",
                claim_token=token,
                error="retry",
                max_attempts=99,
            )

    recovered = okf.recover_stale_claims(
        tmp_path,
        stale_after_seconds=60,
        max_attempts=3,
        now="2026-07-09T18:10:00Z",
    )

    assert recovered == 1
    assert okf.pending_candidates(tmp_path, limit=1) == []
    assert okf.queue_counts(tmp_path) == {"error": 1}


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
    max_generation_seconds: 240
    min_use_count: 3
""",
    )

    cfg = plugin._runtime_config()

    assert cfg.okf.enabled is False
    assert cfg.okf.auto_generate is True
    assert cfg.okf.max_candidates_per_session == 4
    assert cfg.okf.max_generation_seconds == 240
    assert cfg.okf.min_use_count == 3
