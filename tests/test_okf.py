from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
    assert row["last_error_message"] is None
    assert "secret" not in db_text(tmp_path)


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


def test_routing_schema_projection_keeps_only_bounded_structural_identity() -> None:
    raw = {
        "type": "object",
        "title": "private title",
        "description": "medical record for alice@example.com token=secret",
        "properties": {
            "query": {
                "type": "string",
                "description": "private query details",
                "default": "private default",
                "examples": ["private example"],
                "enum": ["private choice"],
            },
            "options": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "uniqueItems": True,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    projected = okf.project_routing_schema(raw)

    assert projected == {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "options": {
                "type": "array",
                "items": {"type": "integer"},
                "uniqueItems": True,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    assert okf.is_routing_schema_projection(projected)
    assert not okf.is_routing_schema_projection({**projected, "description": "private"})
    assert not okf.is_routing_schema_projection({"type": [{}]})


@pytest.mark.parametrize("key", ("items", "contains", "if", "then", "else", "not", "propertyNames"))
@pytest.mark.parametrize("invalid_child", ("PRIVATE_CANARY_7F3A", 1, False, [], None))
def test_schema_projection_rejects_non_mapping_single_children(key: str, invalid_child: Any) -> None:
    assert okf.project_routing_schema({key: invalid_child}) == {}
    assert not okf.is_routing_schema_projection({key: invalid_child})


@pytest.mark.parametrize("key", ("additionalProperties", "unevaluatedProperties"))
def test_schema_projection_allows_bool_or_recursive_mapping_for_boolean_schema_children(key: str) -> None:
    assert okf.is_routing_schema_projection({key: False})
    assert okf.is_routing_schema_projection({key: {"items": {"type": "string"}}})
    assert not okf.is_routing_schema_projection({key: {"items": "PRIVATE_CANARY_7F3A"}})


def test_new_error_admission_persists_error_class_but_no_message(tmp_path: Path) -> None:
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="terminal",
        toolset="terminal",
        schema={"type": "object"},
        args={"command": "private command"},
        success=False,
        error_type="tool_timeout",
        error_message="token=private-secret private command",
    )

    row = okf.pending_candidates(tmp_path, limit=1)[0]
    assert row["last_error_type"] == "tool_timeout"
    assert row["last_error_message"] is None
    assert "private-secret" not in db_text(tmp_path)


def test_fixed_generation_lease_duration_and_owner_fencing(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    assert okf.generation_lease_seconds(10) == 300
    assert okf.generation_lease_seconds(120) == 360
    assert okf.acquire_generation_lease(state_dir, owner="first", lease_seconds=360, now=1_000.0)
    assert not okf.acquire_generation_lease(state_dir, owner="second", lease_seconds=360, now=1_359.0)
    assert okf.acquire_generation_lease(state_dir, owner="second", lease_seconds=360, now=1_360.0)
    assert not okf.release_generation_lease(state_dir, owner="first")
    assert okf.release_generation_lease(state_dir, owner="second")


def test_worker_uses_one_structured_call_for_one_bounded_batch(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "hermes"
    state_dir = tmp_path / "state"
    repo.mkdir()
    hermes_home.mkdir()
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
  okf:
    enabled: true
    auto_generate: true
    max_candidates_per_session: 2
    max_generation_seconds: 120
    min_use_count: 1
""",
    )
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    for tool_name in ("alpha_tool", "beta_tool", "gamma_tool"):
        okf.upsert_tool_candidate(
            state_dir,
            tool_name=tool_name,
            toolset="demo",
            schema={"type": "object", "properties": {"query": {"type": "string"}}},
            args={"query": "private value"},
        )
    calls: list[dict[str, Any]] = []

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            packets = json.loads(kwargs["input"][0]["text"])["candidates"]
            assert len(packets) == 2
            assert all(
                set(packet)
                == {"tool", "toolset", "schema_hash", "schema", "allowed_related_tools", "arg_shape"}
                for packet in packets
            )
            return SimpleNamespace(
                parsed={
                    "okfs": [
                        {
                            "tool": packet["tool"],
                            "schema_hash": packet["schema_hash"],
                            "title": f"Route {packet['tool']}",
                            "aliases": [f"route demo requests with {packet['tool']}"],
                            "triggers": [f"use {packet['tool']} for demo operations"],
                            "when_not_to_use": [],
                            "related_tools": [],
                            "body": f"Use {packet['tool']} for matching demo operations.",
                        }
                        for packet in packets
                    ]
                }
            )

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    assert len(calls) == 1
    assert calls[0]["timeout"] == 120
    assert calls[0]["purpose"] == "local_knowledge.okf_generation"
    assert "private value" not in json.dumps(calls[0], sort_keys=True)
    assert okf.queue_counts(state_dir) == {"done": 2, "pending": 1}
    assert len(list(okf.okf_dir(state_dir).glob("*.md"))) == 2
    assert list(okf.okf_dir(state_dir).glob(".*.tmp")) == []


def configure_auto_generation(
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_candidates: int = 2,
    enabled: bool = True,
    auto_generate: bool = True,
) -> tuple[Path, Path]:
    source_root = base / "repo"
    hermes_home = base / "hermes"
    state_dir = base / "state"
    source_root.mkdir(parents=True)
    hermes_home.mkdir(parents=True)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {source_root}
  state_dir: {state_dir}
  okf:
    enabled: {str(enabled).lower()}
    auto_generate: {str(auto_generate).lower()}
    max_candidates_per_session: {max_candidates}
    max_generation_seconds: 120
    min_use_count: 1
""",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    return hermes_home, state_dir


def generated_item(packet: dict[str, Any], *, label: str = "generated") -> dict[str, Any]:
    return {
        "tool": packet["tool"],
        "schema_hash": packet["schema_hash"],
        "title": f"{label} router for {packet['tool']}",
        "aliases": [f"route demo requests through {packet['tool']}"],
        "triggers": [f"use {packet['tool']} for matching demo operations"],
        "when_not_to_use": [],
        "related_tools": [],
        "body": f"Use {packet['tool']} for matching demo operations.",
    }


def test_worker_normalizes_only_claimed_v0312_schema_projection(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)
    raw_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search phrase",
                "default": "all records",
                "enum": ["recent", "archived"],
                "minLength": 1,
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    legacy_projection = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": {"redacted": True, "type": "str"},
                "default": {"redacted": True, "type": "str"},
                "enum": {"redacted": True, "type": "list", "count": 2},
                "minLength": 1,
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    expected_projection = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    for tool_name, observations in (("selected_legacy_tool", 2), ("unselected_legacy_tool", 1)):
        for _ in range(observations):
            okf.upsert_tool_candidate(
                state_dir,
                tool_name=tool_name,
                toolset="demo",
                schema=raw_schema,
                args={},
            )
    # This is the exact serialization emitted by v0.3.12 canonical_schema_json(safe_schema_view(...)).
    selected_legacy_json = json.dumps(legacy_projection, sort_keys=True, separators=(",", ":"))
    unselected_legacy_json = selected_legacy_json
    raw_hash = okf.schema_hash(raw_schema)
    with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
        conn.executemany(
            "UPDATE okf_candidates SET schema_json = ? WHERE tool_name = ?",
            (
                (selected_legacy_json, "selected_legacy_tool"),
                (unselected_legacy_json, "unselected_legacy_tool"),
            ),
        )

    packets: list[dict[str, Any]] = []

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            batch = json.loads(kwargs["input"][0]["text"])["candidates"]
            assert len(batch) == 1
            packet = batch[0]
            packets.append(packet)
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    assert len(packets) == 1
    assert set(packets[0]) == {
        "tool",
        "toolset",
        "schema_hash",
        "schema",
        "allowed_related_tools",
        "arg_shape",
    }
    assert packets[0]["tool"] == "selected_legacy_tool"
    assert packets[0]["schema_hash"] == raw_hash
    assert packets[0]["schema"] == expected_projection
    assert okf.is_routing_schema_projection(packets[0]["schema"])

    with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
        selected = conn.execute(
            "SELECT schema_json, schema_hash, status FROM okf_candidates WHERE tool_name = ?",
            ("selected_legacy_tool",),
        ).fetchone()
        unselected = conn.execute(
            "SELECT schema_json, schema_hash, status FROM okf_candidates WHERE tool_name = ?",
            ("unselected_legacy_tool",),
        ).fetchone()
    assert selected == (
        json.dumps(expected_projection, sort_keys=True, separators=(",", ":")),
        raw_hash,
        "done",
    )
    assert unselected == (unselected_legacy_json, raw_hash, "pending")
    assert unselected[0].encode("utf-8") == unselected_legacy_json.encode("utf-8")
    for legacy_key in ("description", "default", "enum", "minLength", "redacted"):
        assert legacy_key not in selected[0]


def test_claim_does_not_treat_property_identity_as_a_legacy_schema_keyword(tmp_path: Path) -> None:
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="property_identity_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    malformed_projection = json.dumps(
        {"type": [{}], "properties": {"description": {"type": "string"}}},
        separators=(",", ":"),
    )
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET schema_json = ? WHERE tool_name = 'property_identity_tool'",
            (malformed_projection,),
        )

    claimed = okf.claim_candidates(tmp_path, limit=1, claim_token="property-identity")

    assert len(claimed) == 1
    assert claimed[0]["schema_json"] == malformed_projection


def test_claim_normalizes_nested_v0312_constraint_keyword(tmp_path: Path) -> None:
    raw_schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1}},
    }
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="constraint_tool",
        toolset="demo",
        schema=raw_schema,
        args={},
    )
    legacy_projection = json.dumps(raw_schema, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            "UPDATE okf_candidates SET schema_json = ? WHERE tool_name = 'constraint_tool'",
            (legacy_projection,),
        )

    claimed = okf.claim_candidates(tmp_path, limit=1, claim_token="legacy-constraint")

    expected_projection = {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
    }
    assert len(claimed) == 1
    assert json.loads(claimed[0]["schema_json"]) == expected_projection
    assert claimed[0]["schema_hash"] == okf.schema_hash(raw_schema)


def valid_claimed_okf(row: dict[str, Any], *, label: str = "Generated") -> str:
    tool_name = str(row["tool_name"])
    toolset = str(row.get("toolset") or "").strip()
    lines = [
        "---",
        "artifact_type: tool_okf",
        f"tool: {json.dumps(tool_name)}",
    ]
    if toolset:
        lines.append(f"toolset: {json.dumps(toolset)}")
    lines.extend(
        [
            f"schema_hash: {json.dumps(str(row['schema_hash']))}",
            f"generator_version: {json.dumps(okf.OKF_GENERATOR_VERSION)}",
            f"title: {json.dumps(f'{label} router for {tool_name}')}",
            "aliases:",
            f"  - {json.dumps(f'route matching demo requests through {tool_name}')}",
            "triggers:",
            f"  - {json.dumps(f'use {tool_name} for matching demo operations')}",
            "when_not_to_use:",
            "related_tools:",
            "---",
            f"# {label} router for {tool_name}",
            "",
            f"Use {tool_name} for matching demo operations.",
            "",
        ]
    )
    return "\n".join(lines)


class HookContext:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}

    def register_tool(self, **kwargs: Any) -> None:
        del kwargs

    def register_skill(self, name: str, skill_md: Path) -> None:
        del name, skill_md

    def register_cli_command(self, **kwargs: Any) -> None:
        del kwargs

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback


def registered_hooks() -> dict[str, Any]:
    ctx = HookContext()
    plugin.register(ctx)
    return ctx.hooks


@pytest.mark.parametrize(
    ("outcome", "success_count", "error_count", "error_type"),
    [
        ({"result": json.dumps({"output": "", "exit_code": 0, "error": None})}, 1, 0, None),
        ({"result": json.dumps({"success": False, "error": None})}, 0, 1, "tool_error"),
        ({"result": json.dumps({"error": "command failed"})}, 0, 1, "tool_error"),
        (
            {
                "status": "success",
                "result": json.dumps({"success": False, "error": "stale fallback"}),
            },
            1,
            0,
            None,
        ),
        (
            {
                "status": "timeout",
                "error_type": "tool_timeout",
                "error_message": "private timeout detail",
                "result": json.dumps({"success": True}),
            },
            0,
            1,
            "tool_timeout",
        ),
    ],
)
def test_registered_post_tool_hook_classifies_canonical_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: dict[str, Any],
    success_count: int,
    error_count: int,
    error_type: str | None,
) -> None:
    _hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch)
    registry = SimpleNamespace(
        get_schema=lambda _tool: {"type": "object"},
        get_toolset_for_tool=lambda _tool: "demo",
    )
    monkeypatch.setitem(sys.modules, "tools.registry", SimpleNamespace(registry=registry))

    registered_hooks()["post_tool_call"](
        tool_name="classification_tool",
        args={"private_argument": "private value"},
        **outcome,
    )

    row = okf.pending_candidates(state_dir, limit=1)[0]
    assert row["success_count"] == success_count
    assert row["error_count"] == error_count
    assert row["last_error_type"] == error_type
    assert row["last_error_message"] is None
    assert "private timeout detail" not in db_text(state_dir)


def test_current_schema_accepts_historical_order_nullable_fields_and_unknown_extras(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = okf.okf_queue_db_path(state_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE okf_candidates (
              extra_column TEXT,
              status TEXT,
              tool_name TEXT PRIMARY KEY,
              generator_version TEXT,
              claim_generator_version TEXT,
              related_tools_json TEXT,
              toolset TEXT,
              schema_hash TEXT,
              schema_json TEXT,
              first_seen TEXT,
              last_seen TEXT,
              use_count INTEGER,
              success_count INTEGER,
              error_count INTEGER,
              last_error_type TEXT,
              last_error_message TEXT,
              arg_shape_json TEXT,
              attempt_count INTEGER,
              claimed_at TEXT,
              claim_token TEXT,
              okf_path TEXT,
              last_attempt_error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE okf_worker_leases (
              expires_at REAL,
              extra_column TEXT,
              name TEXT PRIMARY KEY,
              owner TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO okf_candidates (
              extra_column, status, tool_name, generator_version, related_tools_json,
              toolset, schema_hash, schema_json, first_seen, last_seen, use_count,
              success_count, error_count, arg_shape_json, attempt_count
            ) VALUES (?, 'pending', ?, ?, '[]', ?, ?, ?, ?, ?, 4, NULL, NULL, ?, NULL)
            """,
            (
                "preserve-me",
                "historical_tool",
                okf.OKF_GENERATOR_VERSION,
                "demo",
                okf.schema_hash({"type": "object"}),
                okf.canonical_schema_json({"type": "object"}),
                "2026-07-01T00:00:00Z",
                "2026-07-01T00:00:00Z",
                json.dumps(okf.safe_arg_shape({})),
            ),
        )

    pending = okf.pending_candidates(state_dir, limit=1)
    assert pending[0]["success_count"] == 0
    assert pending[0]["attempt_count"] == 0
    claimed = okf.claim_candidates(state_dir, limit=1, claim_token="current-copy")
    assert [row["tool_name"] for row in claimed] == ["historical_tool"]
    assert claimed[0]["claim_token"] == "current-copy"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT extra_column FROM okf_candidates WHERE tool_name = 'historical_tool'"
        ).fetchone() == ("preserve-me",)


def test_same_identity_preserves_lifecycle_while_changed_identity_resets_it(tmp_path: Path) -> None:
    schema = {"type": "object"}
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="identity_tool",
        toolset="demo",
        schema=schema,
        args={},
    )
    assert okf.claim_candidates(tmp_path, limit=1, claim_token="stable-claim")

    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="identity_tool",
        toolset="demo",
        schema=schema,
        args={"private": "value"},
    )
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        same = conn.execute(
            "SELECT status, claim_token, attempt_count, use_count FROM okf_candidates"
        ).fetchone()
    assert same == ("claimed", "stable-claim", 1, 2)

    okf.upsert_tool_candidate(
        tmp_path,
        tool_name="identity_tool",
        toolset="demo",
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
        args={},
    )
    reset = okf.pending_candidates(tmp_path, limit=1)[0]
    assert reset["status"] == "pending"
    assert reset["claim_token"] is None
    assert reset["attempt_count"] == 0
    assert reset["related_tools_json"] == "[]"
    assert reset["okf_path"] is None


@pytest.mark.parametrize(
    ("status", "toolset", "attempt_count", "expected_status", "expected_generator", "expected_attempt", "reset"),
    [
        ("done", "demo", 3, "pending", okf.OKF_GENERATOR_VERSION, 0, True),
        ("pending", None, 2, "pending", okf.OKF_GENERATOR_VERSION, 2, False),
        ("error", "demo", 3, "error", okf.OKF_GENERATOR_VERSION, 3, False),
        ("claimed", None, 1, "claimed", "2", 1, False),
    ],
)
def test_generator_version_drift_preserves_current_schema_lifecycle_matrix(
    tmp_path: Path,
    status: str,
    toolset: str | None,
    attempt_count: int,
    expected_status: str,
    expected_generator: str,
    expected_attempt: int,
    reset: bool,
) -> None:
    schema = {"type": "object"}
    first_seen = "2026-07-01T00:00:00Z"
    last_seen = "2026-07-01T00:02:00Z"
    tool_name = f"{status}_versioned_tool"
    claimed_at = "2026-07-01T00:01:00Z"
    claim_token = f"{status}-claim"
    okf_path = f"{status}.md"
    okf.upsert_tool_candidate(
        tmp_path,
        tool_name=tool_name,
        toolset=toolset,
        schema=schema,
        args={},
        now=first_seen,
    )
    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.execute(
            """
            UPDATE okf_candidates
            SET status = ?, generator_version = '2', attempt_count = ?,
                claimed_at = ?, claim_token = ?,
                claim_generator_version = '2', related_tools_json = '["peer_tool"]',
                okf_path = ?, last_attempt_error = '<redacted>',
                schema_json = '{"type":"string"}', arg_shape_json = '{"type":"str"}',
                use_count = 7, success_count = 5, error_count = 2,
                last_error_type = 'PriorError'
            WHERE tool_name = ?
            """,
            (status, attempt_count, claimed_at, claim_token, okf_path, tool_name),
        )

    okf.upsert_tool_candidate(
        tmp_path,
        tool_name=tool_name,
        toolset=toolset,
        schema=schema,
        args={"query": "private value"},
        now=last_seen,
    )

    with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT status, generator_version, attempt_count, claimed_at, claim_token,
                   claim_generator_version, related_tools_json, okf_path, last_attempt_error,
                   toolset, schema_hash, schema_json, arg_shape_json,
                   first_seen, last_seen, use_count, success_count, error_count,
                   last_error_type, last_error_message
            FROM okf_candidates WHERE tool_name = ?
            """,
            (tool_name,),
        ).fetchone()
    assert row is not None
    assert row["status"] == expected_status
    assert row["generator_version"] == expected_generator
    assert row["attempt_count"] == expected_attempt
    assert row["claimed_at"] == (None if reset else claimed_at)
    assert row["claim_token"] == (None if reset else claim_token)
    assert row["claim_generator_version"] == (None if reset else "2")
    assert row["related_tools_json"] == ("[]" if reset else '["peer_tool"]')
    assert row["okf_path"] == (None if reset else okf_path)
    assert row["last_attempt_error"] == (None if reset else "<redacted>")
    assert row["toolset"] == toolset
    assert row["schema_hash"] == okf.schema_hash(schema)
    assert row["schema_json"] == okf.canonical_schema_json(schema)
    assert row["arg_shape_json"] == json.dumps(
        okf.safe_arg_shape({"query": "private value"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert row["first_seen"] == first_seen
    assert row["last_seen"] == last_seen
    assert row["use_count"] == 8
    assert row["success_count"] == 6
    assert row["error_count"] == 2
    assert row["last_error_type"] == "PriorError"
    assert row["last_error_message"] is None

    if status == "done":
        with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
            conn.execute(
                """
                UPDATE okf_candidates
                SET attempt_count = 2, related_tools_json = '["after_reset"]',
                    okf_path = 'retry.md', last_attempt_error = '<redacted>'
                WHERE tool_name = ?
                """,
                (tool_name,),
            )
        okf.upsert_tool_candidate(
            tmp_path,
            tool_name=tool_name,
            toolset=toolset,
            schema=schema,
            args={},
            now="2026-07-01T00:03:00Z",
        )
        with sqlite3.connect(okf.okf_queue_db_path(tmp_path)) as conn:
            second = conn.execute(
                """
                SELECT status, generator_version, attempt_count, related_tools_json,
                       okf_path, last_attempt_error, first_seen, use_count, success_count, error_count
                FROM okf_candidates WHERE tool_name = ?
                """,
                (tool_name,),
            ).fetchone()
        assert second == (
            "pending",
            okf.OKF_GENERATOR_VERSION,
            2,
            '["after_reset"]',
            "retry.md",
            "<redacted>",
            first_seen,
            9,
            7,
            2,
        )


def test_claim_order_batch_token_and_relation_snapshot_are_deterministic(tmp_path: Path) -> None:
    for tool_name, observations, first_seen in (
        ("alpha_tool", 1, "2026-07-01T00:03:00Z"),
        ("beta_tool", 2, "2026-07-01T00:02:00Z"),
        ("gamma_tool", 2, "2026-07-01T00:01:00Z"),
    ):
        for index in range(observations):
            okf.upsert_tool_candidate(
                tmp_path,
                tool_name=tool_name,
                toolset="demo",
                schema={"type": "object"},
                args={},
                now=(
                    first_seen
                    if index == 0
                    else ("2026-07-01T00:05:00Z" if tool_name == "beta_tool" else "2026-07-01T00:04:00Z")
                ),
            )

    claimed = okf.claim_candidates(tmp_path, limit=2, claim_token="one-batch")

    assert [row["tool_name"] for row in claimed] == ["gamma_tool", "beta_tool"]
    assert {row["claim_token"] for row in claimed} == {"one-batch"}
    assert okf.candidate_packet(claimed[0], tmp_path)["allowed_related_tools"] == [
        "beta_tool",
        "alpha_tool",
    ]


def test_admission_and_worker_packet_exclude_raw_schema_args_result_and_error_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)
    schema = {
        "type": "object",
        "description": "private medical note for alice@example.com token=private-schema",
        "properties": {
            "query": {
                "type": "string",
                "default": "private schema default",
                "examples": ["private schema example"],
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    registry = SimpleNamespace(
        get_schema=lambda _tool: schema,
        get_toolset_for_tool=lambda _tool: "demo",
    )
    monkeypatch.setitem(sys.modules, "tools.registry", SimpleNamespace(registry=registry))
    hook = registered_hooks()["post_tool_call"]
    hook(
        tool_name="privacy_tool",
        args={"private_argument_name": "private argument value"},
        result=json.dumps({"success": False, "error": "private result token=private-error"}),
    )

    row = okf.pending_candidates(state_dir, limit=1)[0]
    assert row["schema_hash"] == okf.schema_hash(schema)
    assert row["last_error_type"] == "tool_error"
    assert row["last_error_message"] is None
    persisted = db_text(state_dir)
    for canary in (
        "alice@example.com",
        "private-schema",
        "private schema default",
        "private schema example",
        "private_argument_name",
        "private argument value",
        "private result",
        "private-error",
    ):
        assert canary not in persisted

    calls: list[dict[str, Any]] = []

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    assert len(calls) == 1
    rendered_call = json.dumps(calls[0], sort_keys=True)
    assert set(json.loads(calls[0]["input"][0]["text"])["candidates"][0]) == {
        "tool",
        "toolset",
        "schema_hash",
        "schema",
        "allowed_related_tools",
        "arg_shape",
    }
    for canary in (
        "alice@example.com",
        "private-schema",
        "private schema default",
        "private schema example",
        "private_argument_name",
        "private argument value",
        "private result",
        "private-error",
    ):
        assert canary not in rendered_call


def test_registered_finalizer_does_not_launch_when_auto_generation_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, auto_generate=False)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="disabled_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned while disabled")),
    )

    assert registered_hooks()["on_session_finalize"](session_id="disabled") is False
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_registered_finalizer_only_spawns_and_leaves_claim_and_model_work_to_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="detached_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    calls: list[list[str]] = []
    reaped = threading.Event()

    class Process:
        pid = 1234

        def wait(self) -> int:
            reaped.set()
            return 0

    def fake_popen(command: list[str], **_kwargs: Any) -> Process:
        calls.append(command)
        return Process()

    class FailLlm:
        def complete_structured(self, **kwargs: Any) -> Any:
            raise AssertionError(f"finalizer called the model inline: {kwargs}")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert registered_hooks()["on_session_finalize"](llm=FailLlm(), session_id="parent") is True
    assert len(calls) == 1
    assert reaped.wait(timeout=1)
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_registered_finalizer_spawn_failure_leaves_work_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="recoverable_spawn_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )

    assert registered_hooks()["on_session_finalize"](session_id="spawn-failure") is False
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_registered_finalizer_does_not_launch_for_fresh_claimed_only_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="fresh_claim_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    assert okf.claim_candidates(state_dir, limit=1, claim_token="fresh-claim")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned for fresh claim")),
    )

    assert registered_hooks()["on_session_finalize"](session_id="fresh") is False
    assert okf.queue_counts(state_dir) == {"claimed": 1}


def test_registered_finalizer_returns_promptly_while_queue_write_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="locked_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned while locked")),
    )
    conn = sqlite3.connect(okf.okf_queue_db_path(state_dir))
    conn.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        launched = registered_hooks()["on_session_finalize"](session_id="locked")
        elapsed = time.monotonic() - started
    finally:
        conn.rollback()
        conn.close()

    assert launched is False
    assert elapsed < 0.5


def test_recursion_guard_skips_admission_and_finalizer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch)
    monkeypatch.setenv(okf.OKF_WORKER_ENV, "1")
    hooks = registered_hooks()

    hooks["post_tool_call"](tool_name="ignored_tool", args={"query": "ignored"}, result="ok")

    assert hooks["on_session_finalize"](session_id="worker") is False
    assert not okf.okf_queue_db_path(state_dir).exists()


def test_finalizer_suppresses_active_lease_and_wakes_for_stale_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="finalizer_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    assert okf.acquire_generation_lease(state_dir, owner="active-worker", lease_seconds=3_600)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned with active lease")),
    )
    finalize = registered_hooks()["on_session_finalize"]
    assert finalize(session_id="active") is False
    assert okf.release_generation_lease(state_dir, owner="active-worker")
    assert okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="stale-claim",
        now="2000-01-01T00:00:00Z",
    )

    calls: list[tuple[list[str], dict[str, Any]]] = []

    class Process:
        pid = 1234

        def wait(self) -> int:
            return 0

    def fake_popen(command: list[str], **kwargs: Any) -> Process:
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert finalize(session_id="stale") is True
    assert len(calls) == 1
    assert okf.queue_counts(state_dir) == {"claimed": 1}


def test_worker_makes_zero_model_calls_when_no_rows_are_claimable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch)

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            raise AssertionError(f"unexpected model call: {kwargs}")

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    assert okf.queue_counts(state_dir) == {}


def test_worker_isolates_malformed_projection_from_valid_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=2)
    canary = "PRIVATE_CANARY_7F3A"
    for tool_name in ("malformed_tool", "valid_tool"):
        okf.upsert_tool_candidate(
            state_dir,
            tool_name=tool_name,
            toolset="demo",
            schema={"items": canary} if tool_name == "malformed_tool" else {"type": "object"},
            args={},
        )
    malformed_schema_json = json.dumps({"items": canary}, separators=(",", ":"))
    with pytest.raises(ValueError, match="neither current nor recognized legacy"):
        okf._claim_schema_projection_json(malformed_schema_json)
    with pytest.raises(ValueError, match="not a valid projection"):
        okf._stored_schema_projection(malformed_schema_json)
    with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
        admitted = conn.execute(
            "SELECT schema_json FROM okf_candidates WHERE tool_name = 'malformed_tool'"
        ).fetchone()
        assert admitted == ("{}",)
        assert canary not in repr(conn.execute("SELECT * FROM okf_candidates").fetchall())
        conn.execute(
            "UPDATE okf_candidates SET schema_json = ? WHERE tool_name = 'malformed_tool'",
            (malformed_schema_json,),
        )

    calls: list[dict[str, Any]] = []

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            packets = json.loads(kwargs["input"][0]["text"])["candidates"]
            calls.append(kwargs)
            assert [packet["tool"] for packet in packets] == ["valid_tool"]
            return SimpleNamespace(parsed={"okfs": [generated_item(packets[0])]})

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    assert len(calls) == 1
    assert canary not in json.dumps(calls, sort_keys=True)
    assert okf.queue_counts(state_dir) == {"done": 1, "pending": 1}
    assert okf.okf_file_path(state_dir, "valid_tool").is_file()
    assert not okf.okf_file_path(state_dir, "malformed_tool").exists()
    malformed = okf.pending_candidates(state_dir, limit=1)[0]
    assert malformed["tool_name"] == "malformed_tool"
    assert malformed["attempt_count"] == 1
    assert malformed["last_attempt_error"] == "<redacted>"
    assert malformed["schema_json"] == malformed_schema_json

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 1
    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 1

    assert len(calls) == 1
    assert okf.queue_counts(state_dir) == {"done": 1, "error": 1}
    with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
        malformed = conn.execute(
            "SELECT status, attempt_count, schema_json FROM okf_candidates WHERE tool_name = 'malformed_tool'"
        ).fetchone()
    assert malformed == ("error", okf.DEFAULT_MAX_ATTEMPTS, malformed_schema_json)
    artifact_text = "".join(
        path.read_text(encoding="utf-8") for path in okf.okf_dir(state_dir).glob("*.md")
    )
    assert canary not in artifact_text
    with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
        non_schema_fields = conn.execute(
            """
            SELECT tool_name, toolset, schema_hash, generator_version, first_seen, last_seen,
                   use_count, success_count, error_count, last_error_type, last_error_message,
                   arg_shape_json, status, attempt_count, claimed_at, claim_token,
                   claim_generator_version, related_tools_json, okf_path, last_attempt_error
            FROM okf_candidates
            """
        ).fetchall()
    assert canary not in repr(non_schema_fields)


def test_worker_model_failure_releases_claim_for_bounded_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="retry_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )

    class Llm:
        def complete_structured(self, **_kwargs: Any) -> Any:
            raise TimeoutError("private provider timeout detail")

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 1
    row = okf.pending_candidates(state_dir, limit=1)[0]
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert row["claim_token"] is None
    assert row["last_attempt_error"] == "<redacted>"
    assert "private provider timeout detail" not in db_text(state_dir)


def test_worker_rejects_cross_toolset_related_links_for_one_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=2)
    for tool_name, toolset in (("cron_tool", "cron"), ("document_tool", "documents")):
        okf.upsert_tool_candidate(
            state_dir,
            tool_name=tool_name,
            toolset=toolset,
            schema={"type": "object"},
            args={},
        )
    calls = 0

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            packets = json.loads(kwargs["input"][0]["text"])["candidates"]
            other = {packets[0]["tool"]: packets[1]["tool"], packets[1]["tool"]: packets[0]["tool"]}
            items = []
            for packet in packets:
                item = generated_item(packet)
                item["related_tools"] = [other[packet["tool"]]]
                items.append(item)
            return SimpleNamespace(parsed={"okfs": items})

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 1
    assert calls == 1
    assert okf.queue_counts(state_dir) == {"pending": 2}
    assert not okf.okf_file_path(state_dir, "cron_tool").exists()
    assert not okf.okf_file_path(state_dir, "document_tool").exists()


def test_worker_skips_pending_work_while_another_unexpired_lease_is_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="leased_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    assert okf.acquire_generation_lease(state_dir, owner="other-worker", lease_seconds=3_600)

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            raise AssertionError(f"model called while another worker owned the lease: {kwargs}")

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    assert okf.queue_counts(state_dir) == {"pending": 1}
    assert not okf.release_generation_lease(state_dir, owner="not-the-owner")
    assert okf.release_generation_lease(state_dir, owner="other-worker")


def test_worker_recovers_invalid_stale_claim_before_generating_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="stale_retry_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    assert okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="abandoned-claim",
        now="2000-01-01T00:00:00Z",
    )
    calls = 0

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    assert calls == 1
    assert okf.queue_counts(state_dir) == {"done": 1}


@pytest.mark.parametrize("case", ["missing", "duplicate", "wrong_hash", "illegal_related", "trivial_routing"])
def test_structured_generation_failures_requeue_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="structured_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    calls = 0

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            item = generated_item(packet)
            if case == "missing":
                items: list[dict[str, Any]] = []
            elif case == "duplicate":
                items = [item, dict(item)]
            elif case == "wrong_hash":
                item["schema_hash"] = "sha256:wrong"
                items = [item]
            elif case == "illegal_related":
                item["related_tools"] = ["unobserved_tool"]
                items = [item]
            else:
                item["aliases"] = ["x"]
                item["triggers"] = ["tool"]
                items = [item]
            return SimpleNamespace(parsed={"okfs": items})

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 1
    assert calls == 1
    assert okf.queue_counts(state_dir) == {"pending": 1}
    assert not okf.okf_file_path(state_dir, "structured_tool").exists()


def test_worker_rejects_slug_collision_without_replacing_existing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    okf.upsert_tool_candidate(
        state_dir,
        tool_name="mcp.tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    target = okf.okf_file_path(state_dir, "mcp.tool")
    original = target.read_bytes()

    okf.upsert_tool_candidate(
        state_dir,
        tool_name="mcp-tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    assert okf.okf_file_path(state_dir, "mcp-tool") == target
    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 1
    assert target.read_bytes() == original
    assert okf.queue_counts(state_dir) == {"done": 1, "pending": 1}


def test_worker_regenerates_existing_artifact_for_same_tool_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)
    tool_name = "same.tool"
    calls = 0

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet, label=f"version {calls}")]})

    okf.upsert_tool_candidate(
        state_dir,
        tool_name=tool_name,
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    target = okf.okf_file_path(state_dir, tool_name)
    first = target.read_bytes()

    okf.upsert_tool_candidate(
        state_dir,
        tool_name=tool_name,
        toolset="demo",
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
        args={},
    )
    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0

    assert calls == 2
    assert target.read_bytes() != first
    assert "version 2 router" in target.read_text(encoding="utf-8")
    assert okf.queue_counts(state_dir) == {"done": 1}
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_expired_worker_cannot_publish_after_successor_takes_over(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="takeover_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    first_calls = 0
    first_result: list[int] = []

    class FirstLlm:
        def complete_structured(self, **kwargs: Any) -> Any:
            nonlocal first_calls
            first_calls += 1
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            first_entered.set()
            assert release_first.wait(timeout=5)
            return SimpleNamespace(parsed={"okfs": [generated_item(packet, label="expired")]})

    first = threading.Thread(
        target=lambda: first_result.append(okf.run_worker(llm=FirstLlm(), hermes_home=hermes_home))
    )
    first.start()
    assert first_entered.wait(timeout=5)
    with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
        conn.execute("UPDATE okf_worker_leases SET expires_at = 0 WHERE name = ?", (okf.GENERATION_LEASE_NAME,))
        conn.execute(
            "UPDATE okf_candidates SET claimed_at = '2000-01-01T00:00:00Z' WHERE tool_name = 'takeover_tool'"
        )

    second_calls = 0

    class SecondLlm:
        def complete_structured(self, **kwargs: Any) -> Any:
            nonlocal second_calls
            second_calls += 1
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet, label="successor")]})

    assert okf.run_worker(llm=SecondLlm(), hermes_home=hermes_home) == 0
    release_first.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert first_result == [1]
    assert first_calls == 1
    assert second_calls == 1
    target = okf.okf_file_path(state_dir, "takeover_tool")
    assert "successor router" in target.read_text(encoding="utf-8")
    assert "expired router" not in target.read_text(encoding="utf-8")
    assert okf.queue_counts(state_dir) == {"done": 1}


def test_worker_recovers_valid_canonical_file_before_attempt_cap_without_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home, state_dir = configure_auto_generation(tmp_path, monkeypatch, max_candidates=1)
    schema = {"type": "object"}
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="recovery_tool",
        toolset="demo",
        schema=schema,
        args={},
    )
    assert okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="crashed-claim",
        max_attempts=1,
        now="2000-01-01T00:00:00Z",
    )
    target = okf.okf_file_path(state_dir, "recovery_tool")
    write(
        target,
        f"""---
artifact_type: tool_okf
tool: recovery_tool
toolset: demo
schema_hash: {okf.schema_hash(schema)}
generator_version: {okf.OKF_GENERATOR_VERSION}
title: Recovery tool router
aliases:
  - route recovery tool requests
triggers:
  - use recovery tool for demo operations
when_not_to_use:
related_tools:
---
# Recovery tool router

Use the recovery tool for matching demo operations.
""",
    )

    class Llm:
        def complete_structured(self, **kwargs: Any) -> Any:
            raise AssertionError(f"unexpected model call: {kwargs}")

    assert okf.run_worker(llm=Llm(), hermes_home=hermes_home) == 0
    assert okf.queue_counts(state_dir) == {"done": 1}
    assert target.exists()
    assert len(okf.index_dirty_tokens(state_dir)) == 1


def test_publication_transaction_serializes_expired_lease_takeover_and_recovery(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="serialized_publication_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    row = okf.claim_candidates(state_dir, limit=1, claim_token="publisher-claim")[0]
    assert okf.acquire_generation_lease(state_dir, owner="publisher", lease_seconds=60)
    target = okf.okf_file_path(state_dir, str(row["tool_name"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.publisher.tmp"
    temp_path.write_text(valid_claimed_okf(row, label="Publisher"), encoding="utf-8")
    publication_entered = threading.Event()
    allow_publication = threading.Event()
    takeover_done = threading.Event()
    publication_result: list[str] = []
    takeover_result: list[tuple[bool, int]] = []

    def publish() -> None:
        temp_path.replace(target)
        publication_entered.set()
        if not allow_publication.wait(timeout=5):
            raise TimeoutError("publication test release timed out")

    def run_publication() -> None:
        publication_result.append(
            okf.publish_claimed_okf(
                state_dir,
                lease_owner="publisher",
                tool_name=str(row["tool_name"]),
                claim_token="publisher-claim",
                okf_path=target,
                publish=publish,
                rollback=lambda: target.unlink(missing_ok=True),
            )
        )

    def run_takeover() -> None:
        acquired = okf.acquire_generation_lease(
            state_dir,
            owner="successor",
            lease_seconds=60,
            now=time.time() + 10_000,
        )
        recovered = okf.recover_stale_claims(
            state_dir,
            stale_after_seconds=1,
            now="2099-01-01T00:00:00Z",
        )
        takeover_result.append((acquired, recovered))
        takeover_done.set()

    publisher = threading.Thread(target=run_publication)
    publisher.start()
    assert publication_entered.wait(timeout=5)
    takeover = threading.Thread(target=run_takeover)
    takeover.start()
    assert not takeover_done.wait(timeout=0.1)
    allow_publication.set()
    publisher.join(timeout=5)
    takeover.join(timeout=5)

    assert not publisher.is_alive()
    assert not takeover.is_alive()
    assert publication_result == ["done"]
    assert takeover_result == [(True, 0)]
    assert okf.queue_counts(state_dir) == {"done": 1}
    assert "Publisher router" in target.read_text(encoding="utf-8")
    assert okf.release_generation_lease(state_dir, owner="successor")


def test_direct_claim_reconciles_valid_stale_artifact_before_attempt_cap(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="direct_recovery_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    row = okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="crashed-direct-claim",
        max_attempts=1,
        now="2000-01-01T00:00:00Z",
    )[0]
    target = okf.okf_file_path(state_dir, str(row["tool_name"]))
    write(target, valid_claimed_okf(row, label="Recovered"))

    replacement = okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="replacement-claim",
        stale_after_seconds=1,
        max_attempts=1,
        now="2099-01-01T00:00:00Z",
    )

    assert replacement == []
    assert okf.queue_counts(state_dir) == {"done": 1}
    assert target.exists()
    assert len(okf.index_dirty_tokens(state_dir)) == 1


def test_direct_claim_waits_for_inflight_publication_before_reconciliation(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="claim_wait_tool",
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    row = okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="inflight-claim",
        now="2000-01-01T00:00:00Z",
    )[0]
    assert okf.acquire_generation_lease(state_dir, owner="publisher", lease_seconds=60)
    target = okf.okf_file_path(state_dir, str(row["tool_name"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.claim-wait.tmp"
    temp_path.write_text(valid_claimed_okf(row, label="Inflight"), encoding="utf-8")
    publication_entered = threading.Event()
    allow_publication = threading.Event()
    claim_done = threading.Event()
    publication_result: list[str] = []
    claim_result: list[list[dict[str, Any]]] = []

    def publish() -> None:
        temp_path.replace(target)
        publication_entered.set()
        if not allow_publication.wait(timeout=5):
            raise TimeoutError("publication test release timed out")

    def run_publication() -> None:
        publication_result.append(
            okf.publish_claimed_okf(
                state_dir,
                lease_owner="publisher",
                tool_name=str(row["tool_name"]),
                claim_token="inflight-claim",
                okf_path=target,
                publish=publish,
                rollback=lambda: target.unlink(missing_ok=True),
            )
        )

    def run_claim() -> None:
        claim_result.append(
            okf.claim_candidates(
                state_dir,
                limit=1,
                claim_token="replacement-claim",
                stale_after_seconds=1,
                max_attempts=1,
                now="2099-01-01T00:00:00Z",
            )
        )
        claim_done.set()

    publisher = threading.Thread(target=run_publication)
    publisher.start()
    assert publication_entered.wait(timeout=5)
    claimant = threading.Thread(target=run_claim)
    claimant.start()
    assert not claim_done.wait(timeout=0.1)
    allow_publication.set()
    publisher.join(timeout=5)
    claimant.join(timeout=5)

    assert not publisher.is_alive()
    assert not claimant.is_alive()
    assert publication_result == ["done"]
    assert claim_result == [[]]
    assert okf.queue_counts(state_dir) == {"done": 1}
    assert target.exists()
    assert okf.release_generation_lease(state_dir, owner="publisher")
