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
    assert "generator_version" in columns
    assert "claim_generator_version" in columns
    assert "related_tools_json" in columns


def test_generator_version_change_requeues_done_candidate_once(tmp_path: Path) -> None:
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
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET generator_version = ?, attempt_count = 3 WHERE tool_name = ?",
            ("legacy", "knowledge_search"),
        )

    assert okf.queue_counts(tmp_path) == {"pending": 1}
    rows = okf.pending_candidates(tmp_path, limit=1)
    assert rows[0]["generator_version"] == okf.OKF_GENERATOR_VERSION
    assert rows[0]["attempt_count"] == 0

    assert okf.queue_counts(tmp_path) == {"pending": 1}


def test_generator_version_migration_preserves_non_done_lifecycle_and_old_claim_identity(tmp_path: Path) -> None:
    for tool_name in ("done_tool", "pending_tool", "claimed_tool", "error_tool"):
        okf.upsert_tool_candidate(
            tmp_path,
            tool_name=tool_name,
            toolset="demo",
            schema={"type": "object"},
            args={},
        )
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET status = 'done', generator_version = 'legacy', "
            "attempt_count = 3, okf_path = '/tmp/old.md' WHERE tool_name = 'done_tool'"
        )
        conn.execute(
            "UPDATE okf_candidates SET status = 'pending', generator_version = 'legacy', "
            "attempt_count = 2 WHERE tool_name = 'pending_tool'"
        )
        conn.execute(
            "UPDATE okf_candidates SET status = 'claimed', generator_version = 'legacy', "
            "claim_generator_version = 'legacy', attempt_count = 1, claim_token = 'old-claim', "
            "claimed_at = '2026-07-24T12:00:00Z' WHERE tool_name = 'claimed_tool'"
        )
        conn.execute(
            "UPDATE okf_candidates SET status = 'error', generator_version = 'legacy', "
            "attempt_count = 3 WHERE tool_name = 'error_tool'"
        )

    assert okf.queue_counts(tmp_path) == {"claimed": 1, "error": 1, "pending": 2}
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = {row["tool_name"]: dict(row) for row in conn.execute("SELECT * FROM okf_candidates")}
    assert rows["done_tool"]["generator_version"] == okf.OKF_GENERATOR_VERSION
    assert rows["done_tool"]["attempt_count"] == 0
    assert rows["done_tool"]["okf_path"] is None
    assert rows["pending_tool"]["generator_version"] == okf.OKF_GENERATOR_VERSION
    assert rows["pending_tool"]["attempt_count"] == 2
    assert rows["error_tool"]["generator_version"] == okf.OKF_GENERATOR_VERSION
    assert rows["error_tool"]["attempt_count"] == 3
    assert rows["claimed_tool"]["generator_version"] == "legacy"
    assert rows["claimed_tool"]["claim_generator_version"] == "legacy"
    assert rows["claimed_tool"]["claim_token"] == "old-claim"

    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="claimed_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.row_factory = sqlite3.Row
        claimed_row = conn.execute(
            "SELECT * FROM okf_candidates WHERE tool_name = 'claimed_tool'"
        ).fetchone()
    assert claimed_row is not None
    assert claimed_row["status"] == "claimed"
    assert claimed_row["generator_version"] == "legacy"
    assert claimed_row["claim_generator_version"] == "legacy"
    assert claimed_row["claim_token"] == "old-claim"

    assert not okf.mark_candidate_done(
        tmp_path,
        tool_name="claimed_tool",
        claim_token="old-claim",
        okf_path=tmp_path / "okfs" / "tools" / "claimed-tool.md",
    )

    assert okf.queue_counts(tmp_path) == {"claimed": 1, "error": 1, "pending": 2}
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET status = 'done', claim_token = NULL, claimed_at = NULL "
            "WHERE tool_name = 'claimed_tool'"
        )

    assert okf.queue_counts(tmp_path) == {"error": 1, "pending": 3}
    rows = {row["tool_name"]: row for row in okf.pending_candidates(tmp_path, limit=10)}
    assert rows["claimed_tool"]["generator_version"] == okf.OKF_GENERATOR_VERSION
    assert rows["claimed_tool"]["attempt_count"] == 0


def test_allowed_related_tools_are_bounded_to_same_toolset(tmp_path: Path) -> None:
    for tool_name, toolset in (
        ("knowledge_search", "local_knowledge"),
        ("knowledge_get", "local_knowledge"),
        ("mcp__paperless__paperless_get_document", "paperless"),
    ):
        okf.upsert_tool_candidate(
            tmp_path,
            tool_name=tool_name,
            toolset=toolset,
            schema={"type": "object"},
            args={},
        )

    rows = {row["tool_name"]: row for row in okf.pending_candidates(tmp_path, limit=10)}

    assert okf.allowed_related_tools(tmp_path, rows["knowledge_search"]) == ["knowledge_get"]


def test_validation_uses_claim_time_related_tool_snapshot_across_ranking_drift(tmp_path: Path) -> None:
    schema = {"type": "object"}
    for _ in range(3):
        okf.upsert_tool_candidate(
            tmp_path,
            tool_name="target_tool",
            toolset="demo",
            schema=schema,
            args={},
        )
    for index in range(okf.DEFAULT_MAX_RELATED_TOOLS + 1):
        okf.upsert_tool_candidate(
            tmp_path,
            tool_name=f"related_{index:02d}",
            toolset="demo",
            schema=schema,
            args={},
        )
    claimed = okf.claim_candidates(tmp_path, limit=1, claim_token="snapshot-claim")
    assert [row["tool_name"] for row in claimed] == ["target_tool"]
    packet = okf.candidate_packet(claimed[0], tmp_path)
    supplied = list(packet["allowed_related_tools"])
    assert len(supplied) == okf.DEFAULT_MAX_RELATED_TOOLS
    all_related = {f"related_{index:02d}" for index in range(okf.DEFAULT_MAX_RELATED_TOOLS + 1)}
    entered = (all_related - set(supplied)).pop()
    dropped = supplied[-1]
    for _ in range(3):
        okf.upsert_tool_candidate(tmp_path, tool_name=entered, toolset="demo", schema=schema, args={})
    current = okf.allowed_related_tools(tmp_path, claimed[0])
    assert entered in current
    assert dropped not in current

    target_path = okf.okf_file_path(tmp_path, "target_tool")

    def content(related_tool: str) -> str:
        return f"""---
artifact_type: tool_okf
tool: target_tool
toolset: demo
schema_hash: {okf.schema_hash(schema)}
generator_version: {okf.OKF_GENERATOR_VERSION}
aliases:
  - operate the target demo tool
triggers:
  - use target tool for demo operation
when_not_to_use:
related_tools:
  - {related_tool}
---

# Target tool

Use the target tool for its matching demo operation.
"""

    write(target_path, content(dropped))
    assert okf.validate_okf_file(tmp_path, claim_token="snapshot-claim", path=target_path)["valid"] is True
    write(target_path, content(entered))
    assert okf.validate_okf_file(tmp_path, claim_token="snapshot-claim", path=target_path)["valid"] is False


def test_validation_rejects_fabricated_toolset_when_claim_has_none(tmp_path: Path) -> None:
    schema = {"type": "object"}
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="unscoped_tool",
        toolset=None,
        schema=schema,
        args={},
    )
    okf.claim_candidates(tmp_path, limit=1, claim_token="unscoped-claim")
    target_path = okf.okf_file_path(tmp_path, "unscoped_tool")
    write(
        target_path,
        f"""---
artifact_type: tool_okf
tool: unscoped_tool
toolset: fabricated
schema_hash: {okf.schema_hash(schema)}
generator_version: {okf.OKF_GENERATOR_VERSION}
aliases:
  - use the unscoped demo tool
triggers:
  - run an unscoped demo operation
when_not_to_use:
related_tools:
---

# Unscoped tool

Use this tool for its matching operation.
""",
    )

    result = okf.validate_okf_file(tmp_path, claim_token="unscoped-claim", path=target_path)

    assert result["valid"] is False
    assert "frontmatter toolset does not match claimed candidate" in result["errors"]


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


def test_toolset_change_requeues_done_candidate(tmp_path: Path) -> None:
    schema = {"type": "object"}
    for case, old_toolset, new_toolset in (
        ("named-to-named", "old_toolset", "new_toolset"),
        ("unscoped-to-named", None, "new_toolset"),
        ("named-to-unscoped", "old_toolset", None),
    ):
        state_dir = tmp_path / case
        okf.upsert_tool_candidate(
            state_dir,
            tool_name="knowledge_search",
            toolset=old_toolset,
            schema=schema,
            args={},
        )
        okf.claim_candidates(state_dir, limit=1, claim_token="claim-toolset")
        assert okf.mark_candidate_done(
            state_dir,
            tool_name="knowledge_search",
            claim_token="claim-toolset",
            okf_path=state_dir / "okfs" / "tools" / "knowledge-search.md",
        )

        okf.upsert_tool_candidate(
            state_dir,
            tool_name="knowledge_search",
            toolset=new_toolset,
            schema=schema,
            args={},
        )

        rows = okf.pending_candidates(state_dir, limit=1)
        assert len(rows) == 1
        assert rows[0]["toolset"] == new_toolset
        assert rows[0]["okf_path"] is None
        assert rows[0]["attempt_count"] == 0
        assert rows[0]["claim_generator_version"] is None
        assert rows[0]["related_tools_json"] == "[]"


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
