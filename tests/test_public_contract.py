from __future__ import annotations

import argparse
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import tomllib
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import MISSING, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO, cast, get_type_hints

import pytest

import hermes_local_knowledge
from hermes_local_knowledge import artifacts as lci_artifacts
from hermes_local_knowledge import cli as lci_cli
from hermes_local_knowledge import config as lci_config
from hermes_local_knowledge import index as lci_index
from hermes_local_knowledge import indexer, plugin


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_INDEXER_API = (
    "Artifact",
    "Edge",
    "IndexSettings",
    "build_index",
    "search_index",
    "get_artifact",
    "get_neighbors",
    "main",
)

EXPECTED_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "knowledge_search": {
        "name": "knowledge_search",
        "description": (
            "Search a local capability index to find the right local skill, "
            "script, runbook, cron job, MCP wrapper, or service doc to inspect first. "
            "Use this before broad file search for local Hermes customizations, "
            "service-operation docs, cron jobs, MCP servers, or project runbooks. Builds "
            "the index automatically when missing. Usage is logged locally for "
            "closed-loop router improvement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query, e.g. 'backup runbook' or 'mcp wrapper'.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": "Maximum results to return. Default 8, max 30.",
                },
                "artifact_type": {
                    "type": "string",
                    "description": (
                        "Optional type filter such as skill, script, runbook, memory_doc, "
                        "cron_job, mcp_server, doc, or skill_support_doc."
                    ),
                },
                "rebuild": {
                    "type": "boolean",
                    "description": (
                        "Force a rebuild of the configured state_dir/index.sqlite before searching. "
                        "Default false."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "knowledge_get": {
        "name": "knowledge_get",
        "description": (
            "Fetch one artifact from the local capability index by id, including "
            "its path, summary, triggers, entities, and related artifact ids. Use after "
            "knowledge_search returns an artifact id. Usage is logged locally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact id such as skill:backup-runbook or cron:daily-review.",
                },
                "include_neighbors": {
                    "type": "boolean",
                    "description": "Also include graph neighbors for this artifact. Default false.",
                },
                "rebuild": {
                    "type": "boolean",
                    "description": (
                        "Force a rebuild of the configured state_dir/index.sqlite before reading. "
                        "Default false."
                    ),
                },
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
    },
    "knowledge_neighbors": {
        "name": "knowledge_neighbors",
        "description": (
            "Return graph neighbors for one local capability artifact. Useful for "
            "jumping from cron jobs to scripts, MCP config entries to wrappers, or "
            "skills to related docs/scripts. Usage is logged locally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact id from knowledge_search or knowledge_get.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum neighbors to return. Default 20, max 50.",
                },
                "rebuild": {
                    "type": "boolean",
                    "description": (
                        "Force a rebuild of the configured state_dir/index.sqlite before reading. "
                        "Default false."
                    ),
                },
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
    },
    "knowledge_feedback": {
        "name": "knowledge_feedback",
        "description": (
            "Record feedback about a local knowledge lookup so future sessions can "
            "improve the capability index. Call this when a result is useful, stale, "
            "missing, noisy, or pointed at the wrong artifact."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rating": {
                    "type": "string",
                    "enum": [
                        "missing",
                        "noisy",
                        "not_useful",
                        "other",
                        "stale",
                        "useful",
                        "wrong_artifact",
                    ],
                    "description": (
                        "Feedback rating: useful, not_useful, missing, noisy, "
                        "wrong_artifact, stale, or other."
                    ),
                },
                "event_id": {
                    "type": "integer",
                    "description": "Optional usage_event_id returned by knowledge_search/get/neighbors.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query being judged, if no event_id is available.",
                },
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact id being judged, if applicable.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "Short concrete note about what worked or what should improve. "
                        "Do not include secrets."
                    ),
                },
            },
            "required": ["rating"],
            "additionalProperties": False,
        },
    },
    "knowledge_usage_report": {
        "name": "knowledge_usage_report",
        "description": (
            "Summarize local knowledge tool usage and feedback to guide self-improvement. "
            "Use before changing index ranking, triggers, docs, or graph edges."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 365,
                    "description": "Lookback window in days. Default 14.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum rows per report section. Default 10.",
                },
            },
            "additionalProperties": False,
        },
    },
}

EXPECTED_TOOL_ORDER = list(EXPECTED_TOOL_SCHEMAS)
EXPECTED_TOOL_EMOJIS = {
    "knowledge_search": "🗺️",
    "knowledge_get": "📄",
    "knowledge_neighbors": "🔗",
    "knowledge_feedback": "📝",
    "knowledge_usage_report": "📊",
}
ARTIFACT_RESULT_KEYS = {
    "id",
    "type",
    "title",
    "path",
    "summary",
    "triggers",
    "entities",
    "related",
    "updated_at",
    "source",
}
SEARCH_RESULT_KEYS = ARTIFACT_RESULT_KEYS | {"rank"}
BASE_INDEX_METADATA_KEYS = {
    "plugin_version",
    "root",
    "source_root_source",
    "state_dir",
    "state_dir_source",
    "include_markdown_docs_source",
    "db_path",
    "warnings",
    "rebuilt",
    "expected_index_format_version",
}
PERSISTED_INDEX_METADATA_KEYS = {
    "index_exists",
    "index_mtime",
    "index_age_seconds",
    "artifact_count",
    "artifact_counts_by_type",
    "edge_count",
    "index_format_version",
}
USAGE_REPORT_KEYS = {
    "success",
    "usage_db_path",
    "live_root",
    "since",
    "days",
    "total_events",
    "live_total_events",
    "feedback_count",
    "live_feedback_count",
    "avg_latency_ms",
    "root_breakdown",
    "feedback_root_breakdown",
    "top_tools",
    "top_queries",
    "zero_result_queries",
    "live_zero_result_queries",
    "unresolved_zero_result_queries",
    "active_zero_result_queries",
    "probe_zero_result_queries",
    "resolved_zero_result_queries",
    "top_artifacts",
    "errors",
    "live_errors",
    "recent_live_errors_since",
    "recent_live_errors",
    "feedback_by_rating",
    "feedback_rating_buckets",
    "unknown_feedback_ratings",
    "recent_negative_feedback",
    "live_recent_negative_feedback",
    "unresolved_negative_feedback",
    "resolved_negative_feedback",
    "latest_index_metadata",
    "recent_builds",
    "improvement_candidates",
    "usage_event_id",
}
EVALUATION_METRIC_KEYS = {
    "query_count",
    "label_count",
    "hit_at_1",
    "hit_at_3",
    "hit_at_5",
    "hit_at_10",
    "mrr_at_10",
    "parent_equiv_hit_at_1",
    "parent_equiv_hit_at_3",
    "parent_equiv_hit_at_5",
    "parent_equiv_hit_at_10",
    "parent_equiv_mrr_at_10",
}

USAGE_EVENT_SCHEMA = [
    ("id", "INTEGER", 0, None, 1),
    ("ts", "TEXT", 1, None, 0),
    ("tool", "TEXT", 1, None, 0),
    ("client", "TEXT", 1, "'native'", 0),
    ("session_id", "TEXT", 0, None, 0),
    ("task_id", "TEXT", 0, None, 0),
    ("tool_call_id", "TEXT", 0, None, 0),
    ("query", "TEXT", 0, None, 0),
    ("artifact_id", "TEXT", 0, None, 0),
    ("artifact_type", "TEXT", 0, None, 0),
    ("limit_value", "INTEGER", 0, None, 0),
    ("rebuild_requested", "INTEGER", 1, "0", 0),
    ("rebuilt", "INTEGER", 0, None, 0),
    ("success", "INTEGER", 1, None, 0),
    ("error", "TEXT", 0, None, 0),
    ("result_count", "INTEGER", 0, None, 0),
    ("top_ids_json", "TEXT", 1, "'[]'", 0),
    ("top_types_json", "TEXT", 1, "'[]'", 0),
    ("latency_ms", "INTEGER", 0, None, 0),
    ("plugin_version", "TEXT", 0, None, 0),
    ("source_root_source", "TEXT", 0, None, 0),
    ("state_dir_source", "TEXT", 0, None, 0),
    ("include_markdown_docs_source", "TEXT", 0, None, 0),
    ("index_exists", "INTEGER", 0, None, 0),
    ("index_mtime", "TEXT", 0, None, 0),
    ("index_age_seconds", "INTEGER", 0, None, 0),
    ("index_artifact_count", "INTEGER", 0, None, 0),
    ("index_edge_count", "INTEGER", 0, None, 0),
    ("index_artifact_counts_json", "TEXT", 1, "'{}'", 0),
    ("index_metadata_error", "TEXT", 0, None, 0),
    ("build_duration_ms", "INTEGER", 0, None, 0),
    ("root", "TEXT", 0, None, 0),
    ("db_path", "TEXT", 0, None, 0),
]
FEEDBACK_SCHEMA = [
    ("id", "INTEGER", 0, None, 1),
    ("ts", "TEXT", 1, None, 0),
    ("event_id", "INTEGER", 0, None, 0),
    ("rating", "TEXT", 1, None, 0),
    ("query", "TEXT", 0, None, 0),
    ("artifact_id", "TEXT", 0, None, 0),
    ("note", "TEXT", 0, None, 0),
    ("session_id", "TEXT", 0, None, 0),
    ("task_id", "TEXT", 0, None, 0),
    ("tool_call_id", "TEXT", 0, None, 0),
    ("root", "TEXT", 0, None, 0),
]
@dataclass(frozen=True)
class Workspace:
    root: Path
    hermes_home: Path
    state_dir: Path


class RecordingContext:
    def __init__(self, *, llm: object | None = None) -> None:
        self.llm = llm
        self.tools: list[dict[str, Any]] = []
        self.skills: list[tuple[str, Path]] = []
        self.cli_commands: list[dict[str, Any]] = []
        self.hooks: dict[str, Callable[..., Any]] = {}

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_skill(self, name: str, skill_md: Path | str) -> None:
        self.skills.append((name, Path(skill_md)))

    def register_cli_command(self, **kwargs: Any) -> None:
        self.cli_commands.append(kwargs)

    def register_hook(self, name: str, handler: Callable[..., Any]) -> None:
        assert name not in self.hooks
        self.hooks[name] = handler


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_process_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home = tmp_path / "process-home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.delenv("HERMES_LOCAL_KNOWLEDGE_OKF_WORKER", raising=False)
    yield


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    root = tmp_path / "source"
    hermes_home = tmp_path / "hermes-home"
    state_dir = tmp_path / "state"
    hermes_home.mkdir()
    write(
        root / "custom_skills" / "quartz-router" / "SKILL.md",
        """---
name: quartz-router
description: Route quartz inventory operations to the canonical local procedure.
tags:
  - quartz
  - inventory
related_skills:
  - quartz-helper
---
# Quartz Router

Use this skill for quartz inventory operations.
""",
    )
    write(
        root / "custom_skills" / "quartz-helper" / "SKILL.md",
        """---
name: quartz-helper
description: Supporting procedures for quartz inventory maintenance.
tags:
  - quartz
  - maintenance
---
# Quartz Helper

Use this helper after selecting the quartz inventory router.
""",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_ROOT", str(root))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_STATE_DIR", str(state_dir))
    return Workspace(root=root, hermes_home=hermes_home, state_dir=state_dir)


def registered_surface(*, llm: object | None = None) -> RecordingContext:
    ctx = RecordingContext(llm=llm)
    plugin.register(ctx)
    return ctx


def tool_call(ctx: RecordingContext, name: str) -> dict[str, Any]:
    return next(call for call in ctx.tools if call["name"] == name)


def invoke_tool(ctx: RecordingContext, name: str, args: Any, **kwargs: Any) -> dict[str, Any]:
    handler = cast(Callable[..., str], tool_call(ctx, name)["handler"])
    payload = json.loads(handler(args, **kwargs))
    assert isinstance(payload, dict)
    json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return payload


def run_cli_json(capsys: pytest.CaptureFixture[str], argv: Sequence[str]) -> tuple[int, dict[str, Any]]:
    status = lci_cli.main(list(argv))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return status, payload


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def create_current_usage_history(state_dir: Path, root: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "usage.sqlite"
    schemas: dict[str, Sequence[tuple[str, str, int, str | None, int]]] = {
        "usage_events": USAGE_EVENT_SCHEMA,
        "feedback": FEEDBACK_SCHEMA,
    }
    with sqlite3.connect(db_path) as conn:
        for table, schema in schemas.items():
            definitions: list[str] = []
            for name, column_type, not_null, default, primary_key in schema:
                parts = [name, column_type]
                if primary_key:
                    parts.append("PRIMARY KEY")
                if not_null:
                    parts.append("NOT NULL")
                if default is not None:
                    parts.extend(("DEFAULT", str(default)))
                definitions.append(" ".join(parts))
            conn.execute(f"CREATE TABLE {table} ({', '.join(definitions)})")
        for statement in (
            "CREATE INDEX idx_usage_events_ts ON usage_events(ts)",
            "CREATE INDEX idx_usage_events_tool ON usage_events(tool)",
            "CREATE INDEX idx_usage_events_query ON usage_events(query)",
            "CREATE INDEX idx_feedback_ts ON feedback(ts)",
            "CREATE INDEX idx_feedback_rating ON feedback(rating)",
        ):
            conn.execute(statement)
        timestamp = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        root_text = str(root.resolve())
        event = {
            "id": 4001,
            "ts": timestamp,
            "tool": "knowledge_search",
            "client": "native",
            "session_id": "seeded-session",
            "query": "seeded historical quartz",
            "artifact_id": "skill:quartz-router",
            "artifact_type": "skill",
            "limit_value": 10,
            "rebuild_requested": 0,
            "rebuilt": 0,
            "success": 1,
            "result_count": 1,
            "top_ids_json": '["skill:quartz-router"]',
            "top_types_json": '["skill"]',
            "latency_ms": 7,
            "plugin_version": "0.3.12",
            "source_root_source": "env",
            "state_dir_source": "env",
            "include_markdown_docs_source": "env",
            "root": root_text,
            "db_path": str((state_dir / "index.sqlite").resolve()),
        }
        columns = ", ".join(event)
        conn.execute(
            f"INSERT INTO usage_events ({columns}) VALUES ({', '.join('?' for _ in event)})",
            tuple(event.values()),
        )
        feedback = {
            "id": 5001,
            "ts": timestamp,
            "event_id": 4001,
            "rating": "useful",
            "query": "seeded historical quartz",
            "artifact_id": "skill:quartz-router",
            "note": "seeded v0.3.12 feedback",
            "session_id": "seeded-session",
            "root": root_text,
        }
        columns = ", ".join(feedback)
        conn.execute(
            f"INSERT INTO feedback ({columns}) VALUES ({', '.join('?' for _ in feedback)})",
            tuple(feedback.values()),
        )
    return db_path


def create_current_okf_queue(
    state_dir: Path,
    *,
    tool_name: str = "public_demo_tool",
    candidates: Sequence[tuple[str, int]] | None = None,
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "okf_queue.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE okf_candidates (
              tool_name TEXT PRIMARY KEY,
              toolset TEXT,
              schema_hash TEXT,
              schema_json TEXT,
              generator_version TEXT,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              use_count INTEGER NOT NULL DEFAULT 0,
              success_count INTEGER NOT NULL DEFAULT 0,
              error_count INTEGER NOT NULL DEFAULT 0,
              last_error_type TEXT,
              last_error_message TEXT,
              arg_shape_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL DEFAULT 'pending',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              claimed_at TEXT,
              claim_token TEXT,
              claim_generator_version TEXT,
              related_tools_json TEXT NOT NULL DEFAULT '[]',
              okf_path TEXT,
              last_attempt_error TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX idx_okf_candidates_status_seen "
            "ON okf_candidates(status, use_count, last_seen)"
        )
        conn.execute(
            """
            CREATE TABLE okf_worker_leases (
              name TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              expires_at REAL NOT NULL
            )
            """
        )
        for candidate_name, use_count in candidates or [(tool_name, 1)]:
            row: dict[str, Any] = {
                "tool_name": candidate_name,
                "toolset": "local_knowledge",
                "schema_hash": "sha256:public-fixture",
                "schema_json": json.dumps(
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "generator_version": "3",
                "first_seen": "2026-07-01T00:00:00Z",
                "last_seen": "2026-07-01T00:00:00Z",
                "use_count": use_count,
                "success_count": use_count,
                "error_count": 0,
                "last_error_type": None,
                "last_error_message": None,
                "arg_shape_json": json.dumps(
                    {
                        "type": "object",
                        "field_count": 1,
                        "fields": {"field_0": {"type": "str"}},
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "status": "pending",
                "attempt_count": 0,
                "claimed_at": None,
                "claim_token": None,
                "claim_generator_version": None,
                "related_tools_json": "[]",
                "okf_path": None,
                "last_attempt_error": None,
            }
            columns = ", ".join(row)
            placeholders = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO okf_candidates ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )
    return db_path


def test_tool_registration_freezes_exact_five_contracts_and_host_integrations(
    workspace: Workspace,
) -> None:
    ctx = registered_surface()

    assert [call["name"] for call in ctx.tools] == EXPECTED_TOOL_ORDER
    assert len(ctx.tools) == 5
    for call in ctx.tools:
        name = str(call["name"])
        assert set(call) == {"name", "toolset", "schema", "handler", "check_fn", "emoji"}
        assert call["toolset"] == "local_knowledge"
        assert call["schema"] == EXPECTED_TOOL_SCHEMAS[name]
        check_fn = cast(Callable[[], bool], call["check_fn"])
        assert check_fn() is True
        assert call["emoji"] == EXPECTED_TOOL_EMOJIS[name]
        assert callable(call["handler"])

    expected_skill = REPO_ROOT / "skills" / "local-knowledge-router" / "SKILL.md"
    assert ctx.skills == [("local-knowledge-router", expected_skill)]
    assert expected_skill.is_file()
    assert set(ctx.hooks) == {"post_tool_call", "on_session_finalize"}
    assert len(ctx.cli_commands) == 1
    cli_registration = ctx.cli_commands[0]
    assert set(cli_registration) == {"name", "help", "description", "setup_fn", "handler_fn"}
    assert cli_registration["name"] == "local-knowledge"
    assert cli_registration["help"] == "Install and diagnose the local knowledge plugin"
    assert cli_registration["description"] == "Install the proactive router skill or check plugin health."
    assert callable(cli_registration["setup_fn"])
    assert callable(cli_registration["handler_fn"])


def test_registered_handlers_return_stable_json_success_envelopes(workspace: Workspace) -> None:
    ctx = registered_surface()

    search = invoke_tool(
        ctx,
        "knowledge_search",
        {"query": "quartz inventory operations", "limit": 5, "rebuild": True},
        session_id="public-contract-session",
    )
    assert set(search) == {
        "success",
        "query",
        "artifact_type",
        "limit",
        "results",
        "usage_event_id",
        "build_duration_ms",
        *BASE_INDEX_METADATA_KEYS,
        *PERSISTED_INDEX_METADATA_KEYS,
    }
    assert search["success"] is True
    assert search["query"] == "quartz inventory operations"
    assert search["artifact_type"] is None
    assert search["limit"] == 5
    assert isinstance(search["usage_event_id"], int)
    assert search["root"] == str(workspace.root.resolve())
    assert search["state_dir"] == str(workspace.state_dir.resolve())
    assert search["source_root_source"] == "env"
    assert search["state_dir_source"] == "env"
    assert search["rebuilt"] is True
    assert search["index_exists"] is True
    assert search["index_format_version"] == search["expected_index_format_version"]
    assert search["artifact_counts_by_type"] == {"skill": 2}
    assert search["results"][0]["id"] == "skill:quartz-router"
    assert set(search["results"][0]) == SEARCH_RESULT_KEYS

    fetched = invoke_tool(
        ctx,
        "knowledge_get",
        {"artifact_id": "skill:quartz-router", "include_neighbors": True},
    )
    assert set(fetched) == {
        "success",
        "artifact",
        "neighbors",
        "usage_event_id",
        *BASE_INDEX_METADATA_KEYS,
        *PERSISTED_INDEX_METADATA_KEYS,
    }
    assert fetched["success"] is True
    assert fetched["artifact"]["id"] == "skill:quartz-router"
    assert set(fetched["artifact"]) == ARTIFACT_RESULT_KEYS
    assert isinstance(fetched["usage_event_id"], int)
    assert [row["id"] for row in fetched["neighbors"]] == ["skill:quartz-helper"]
    assert set(fetched["neighbors"][0]) == ARTIFACT_RESULT_KEYS | {"edge_kind", "edge_evidence"}

    neighbors = invoke_tool(ctx, "knowledge_neighbors", {"artifact_id": "skill:quartz-router"})
    assert set(neighbors) == {
        "success",
        "artifact_id",
        "neighbors",
        "limit",
        "usage_event_id",
        *BASE_INDEX_METADATA_KEYS,
        *PERSISTED_INDEX_METADATA_KEYS,
    }
    assert neighbors["success"] is True
    assert neighbors["artifact_id"] == "skill:quartz-router"
    assert neighbors["limit"] == 20
    assert isinstance(neighbors["usage_event_id"], int)
    assert [row["id"] for row in neighbors["neighbors"]] == ["skill:quartz-helper"]

    option_cases: list[tuple[str, dict[str, Any], dict[str, Any], tuple[str, ...]]] = [
        (
            "knowledge_search",
            {"query": "quartz", "artifact_type": "script", "rebuild": False},
            {"artifact_type": "script", "results": [], "rebuilt": False},
            (),
        ),
        (
            "knowledge_get",
            {"artifact_id": "skill:quartz-router", "rebuild": True},
            {"rebuilt": True},
            ("neighbors",),
        ),
        (
            "knowledge_neighbors",
            {"artifact_id": "skill:quartz-router", "limit": 1, "rebuild": True},
            {"limit": 1, "rebuilt": True},
            (),
        ),
    ]
    for tool_name, args, expected, absent_keys in option_cases:
        payload = invoke_tool(ctx, tool_name, args)
        assert payload["success"] is True
        for key, value in expected.items():
            assert payload[key] == value
        for key in absent_keys:
            assert key not in payload

    feedback = invoke_tool(
        ctx,
        "knowledge_feedback",
        {
            "rating": "useful",
            "event_id": search["usage_event_id"],
            "query": "quartz inventory operations",
            "artifact_id": "skill:quartz-router",
            "note": "public fixture routed correctly",
        },
    )
    assert set(feedback) == {
        "success",
        "feedback_id",
        "usage_event_id",
        "rating",
        "event_id",
        "usage_db_path",
    }
    assert feedback["success"] is True
    assert isinstance(feedback["feedback_id"], int)
    assert isinstance(feedback["usage_event_id"], int)
    assert feedback["event_id"] == search["usage_event_id"]
    assert feedback["usage_db_path"] == str(workspace.state_dir.resolve() / "usage.sqlite")

    report = invoke_tool(ctx, "knowledge_usage_report", {"days": 30, "limit": 10})
    assert set(report) == USAGE_REPORT_KEYS
    assert report["success"] is True
    assert report["total_events"] == 7
    assert report["live_total_events"] == 7
    assert report["feedback_count"] == 1
    assert report["live_feedback_count"] == 1
    assert isinstance(report["usage_event_id"], int)


def test_registered_handlers_keep_expected_bad_input_inside_error_envelopes(
    workspace: Workspace,
) -> None:
    ctx = registered_surface()

    for name in EXPECTED_TOOL_ORDER:
        payload = invoke_tool(ctx, name, None)
        assert payload == {"error": "args must be an object", "success": False}
        assert "usage_event_id" not in payload

    validation_cases: list[tuple[str, Any, str]] = [
        ("knowledge_search", {}, "query is required"),
        ("knowledge_get", {"artifact_id": ""}, "artifact_id is required"),
        ("knowledge_neighbors", {"artifact_id": None}, "artifact_id is required"),
        ("knowledge_feedback", {"rating": "great"}, "rating must be one of"),
        (
            "knowledge_feedback",
            {"rating": "useful", "event_id": []},
            "event_id must be an integer when provided",
        ),
    ]
    for name, args, expected_error in validation_cases:
        payload = invoke_tool(ctx, name, args)
        assert payload["success"] is False
        assert expected_error in payload["error"]
        assert "usage_event_id" not in payload

    coerced = invoke_tool(
        ctx,
        "knowledge_search",
        {"query": "quartz inventory", "limit": "not-an-integer", "rebuild": []},
    )
    assert coerced["success"] is True
    assert coerced["limit"] == 8
    assert isinstance(coerced["usage_event_id"], int)

    missing = invoke_tool(ctx, "knowledge_get", {"artifact_id": "skill:missing"})
    assert missing["success"] is False
    assert missing["error"] == "Artifact not found: skill:missing"
    assert missing["artifact_id"] == "skill:missing"
    assert isinstance(missing["usage_event_id"], int)


def test_standalone_cli_default_result_limits_are_observable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "source"
    hermes_home = tmp_path / "hermes-home"
    state_dir = tmp_path / "state"
    hermes_home.mkdir()
    for index in range(12):
        write(
            root / "custom_skills" / f"limit-marker-{index:02d}" / "SKILL.md",
            f"""---
name: limit-marker-{index:02d}
description: Limitmarker public contract capability {index}.
---
# Limit marker {index}
""",
        )
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {state_dir}
""",
    )

    assert lci_cli.main(
        ["build", "--hermes-home", str(hermes_home), "--from-hermes-config"]
    ) == 0
    capsys.readouterr()
    db_path = state_dir / "index.sqlite"

    assert lci_cli.main(["search", "limitmarker", "--db", str(db_path), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 10

    status, doctor = run_cli_json(
        capsys,
        ["doctor", "--hermes-home", str(hermes_home), "--query", "limitmarker", "--json"],
    )
    assert status == 0
    assert doctor["rebuilt"] is False
    assert doctor["smoke_result_count"] == 5

    queue_state = tmp_path / "queue-state"
    create_current_okf_queue(
        queue_state,
        candidates=[(f"limit_tool_{index:02d}", 1) for index in range(12)],
    )
    common = ["--state-dir", str(queue_state), "--json"]
    status, queue = run_cli_json(capsys, ["okf", "status", *common])
    assert status == 0
    assert queue["counts"] == {"pending": 12}
    assert len(queue["pending"]) == 10

    status, claim = run_cli_json(capsys, ["okf", "claim", *common])
    assert status == 0
    assert claim["count"] == 1


@pytest.mark.parametrize(
    ("prefix", "snippets"),
    [
        (
            [],
            [
                "build index.sqlite and index.jsonl",
                "search artifacts",
                "show one artifact by id",
                "show graph neighbors for one artifact",
                "replay positive feedback labels against the current search index",
                "check runtime config, paths, index presence, and optional smoke build/search",
                "install the bundled router skill into the active Hermes profile",
                "manage generated tool OKF candidate queue",
            ],
        ),
        (
            ["build"],
            ["--root ROOT", "--hermes-home HERMES_HOME", "--output-dir OUTPUT_DIR", "--from-hermes-config"],
        ),
        (
            ["search"],
            ["query", "--limit LIMIT", "--json", "--db DB", "--hermes-home HERMES_HOME", "--from-hermes-config"],
        ),
        (["get"], ["artifact_id", "--json", "--db DB", "--from-hermes-config"]),
        (["neighbors"], ["artifact_id", "--json", "--db DB", "--from-hermes-config"]),
        (
            ["evaluate"],
            ["--usage-db USAGE_DB", "--json", "--details", "--db DB", "--from-hermes-config"],
        ),
        (
            ["doctor"],
            ["--hermes-home HERMES_HOME", "--rebuild", "--query QUERY", "--limit LIMIT", "--json"],
        ),
        (
            ["install-router-skill"],
            ["--hermes-home HERMES_HOME", "--force", "--json"],
        ),
        (
            ["okf"],
            [
                "show OKF queue status",
                "claim pending OKF candidates",
                "validate an authored OKF file",
                "mark a claimed OKF candidate complete",
                "mark a claimed OKF candidate failed",
                "reset an exhausted OKF candidate for retry",
            ],
        ),
        (
            ["okf", "status"],
            ["--limit LIMIT", "--hermes-home HERMES_HOME", "--from-hermes-config", "--state-dir STATE_DIR", "--json"],
        ),
        (
            ["okf", "claim"],
            ["--limit LIMIT", "--min-use-count MIN_USE_COUNT", "--claim-token CLAIM_TOKEN", "--state-dir STATE_DIR"],
        ),
        (
            ["okf", "validate"],
            ["--claim-token CLAIM_TOKEN", "--path PATH", "--state-dir STATE_DIR", "--json"],
        ),
        (
            ["okf", "complete"],
            ["--claim-token CLAIM_TOKEN", "--tool TOOL", "--path PATH", "--json"],
        ),
        (
            ["okf", "fail"],
            ["--claim-token CLAIM_TOKEN", "--tool TOOL", "--error ERROR", "--json"],
        ),
        (["okf", "retry"], ["--tool TOOL", "--state-dir STATE_DIR", "--json"]),
    ],
)
def test_standalone_cli_help_contract(
    prefix: list[str],
    snippets: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        lci_cli.main([*prefix, "--help"])
    assert exc_info.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    for snippet in snippets:
        assert snippet in output


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["unknown"],
        ["search"],
        ["get"],
        ["neighbors"],
        ["okf"],
        ["okf", "validate"],
        ["okf", "complete", "--claim-token", "token"],
        ["okf", "fail", "--claim-token", "token", "--tool", "demo"],
        ["okf", "retry"],
    ],
)
def test_standalone_cli_parse_errors_exit_two(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        lci_cli.main(argv)
    assert exc_info.value.code == 2
    assert capsys.readouterr().err


def test_public_indexer_api_names_call_shapes_and_model_defaults_are_frozen() -> None:
    assert indexer.__all__ == list(PUBLIC_INDEXER_API)
    assert all(hasattr(indexer, name) for name in PUBLIC_INDEXER_API)
    assert indexer.Artifact is lci_artifacts.Artifact
    assert indexer.Edge is lci_artifacts.Edge
    assert indexer.IndexSettings is lci_config.IndexSettings
    assert indexer.build_index is lci_index.build_index
    assert indexer.search_index is lci_index.search_index
    assert indexer.get_artifact is lci_index.get_artifact
    assert indexer.get_neighbors is lci_index.get_neighbors
    assert indexer.main is lci_cli.main

    def assert_parameter(
        callable_obj: Callable[..., Any],
        name: str,
        kind: Any,
        default: Any = inspect.Signature.empty,
    ) -> None:
        parameter = inspect.signature(callable_obj).parameters[name]
        assert parameter.kind is kind
        assert parameter.default is default or parameter.default == default

    positional = inspect.Parameter.POSITIONAL_OR_KEYWORD
    keyword_only = inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(indexer.build_index).parameters) == [
        "root",
        "output_dir",
        "hermes_home",
        "settings",
        "force",
    ]
    for name in ("root", "output_dir", "hermes_home"):
        assert_parameter(indexer.build_index, name, positional)
    assert_parameter(indexer.build_index, "settings", positional, None)
    assert_parameter(indexer.build_index, "force", keyword_only, True)
    assert get_type_hints(indexer.build_index)["return"] == (
        tuple[list[indexer.Artifact], list[indexer.Edge]] | None
    )
    assert_parameter(indexer.search_index, "db_path", positional)
    assert_parameter(indexer.search_index, "query", positional)
    assert_parameter(indexer.search_index, "limit", keyword_only, 10)
    assert_parameter(indexer.search_index, "artifact_type", keyword_only, None)
    for lookup in (indexer.get_artifact, indexer.get_neighbors):
        assert_parameter(lookup, "db_path", positional)
        assert_parameter(lookup, "artifact_id", positional)
    assert_parameter(indexer.main, "argv", positional, None)

    root = Path("source")
    state_dir = Path("state")
    hermes_home = Path("hermes-home")
    inspect.signature(indexer.build_index).bind(root, state_dir, hermes_home)
    inspect.signature(indexer.build_index).bind(
        root=root,
        output_dir=state_dir,
        hermes_home=hermes_home,
        settings=indexer.IndexSettings(),
    )
    inspect.signature(indexer.search_index).bind(
        db_path=state_dir / "index.sqlite",
        query="quartz",
        limit=1,
        artifact_type="skill",
    )
    inspect.signature(indexer.get_artifact).bind(state_dir / "index.sqlite", "skill:demo")
    inspect.signature(indexer.get_neighbors).bind(
        db_path=state_dir / "index.sqlite",
        artifact_id="skill:demo",
    )
    inspect.signature(indexer.main).bind()
    inspect.signature(indexer.main).bind(["--help"])

    artifact_fields = fields(indexer.Artifact)
    assert [field.name for field in artifact_fields] == [
        "id",
        "type",
        "title",
        "path",
        "summary",
        "triggers",
        "entities",
        "related",
        "updated_at",
        "source",
        "search_text",
    ]
    assert all(field.default is MISSING for field in artifact_fields[:5])
    assert all(field.default_factory is list for field in artifact_fields[5:8])
    assert [field.default for field in artifact_fields[8:]] == [None, None, ""]
    artifact = indexer.Artifact("id", "skill", "Title", "path", "summary")
    assert artifact.triggers == []
    assert artifact.entities == []
    assert artifact.related == []

    edge_fields = fields(indexer.Edge)
    assert [field.name for field in edge_fields] == ["source", "target", "kind", "evidence"]
    assert all(field.default is MISSING for field in edge_fields)

    settings = indexer.IndexSettings()
    assert settings.custom_skill_dirs == ("custom_skills",)
    assert settings.script_dirs == ("scripts", "hermes_home/scripts")
    assert settings.memory_dirs == ("memory",)
    assert settings.runbook_dirs == ("docs",)
    assert settings.known_entities == ("Hermes", "GitHub", "MCP", "Cron")
    assert settings.include_markdown_docs is True
    assert settings.exclude_dir_names == ()


def test_public_indexer_api_build_search_get_neighbors_and_jsonl_contract(
    workspace: Workspace,
) -> None:
    artifacts, edges = indexer.build_index(
        workspace.root,
        workspace.state_dir,
        workspace.hermes_home,
    )
    assert {artifact.id for artifact in artifacts} == {
        "skill:quartz-helper",
        "skill:quartz-router",
    }
    assert all(isinstance(artifact, indexer.Artifact) for artifact in artifacts)
    assert edges == [
        indexer.Edge(
            source="skill:quartz-router",
            target="skill:quartz-helper",
            kind="related_to",
            evidence="skill:quartz-helper",
        )
    ]
    assert (workspace.state_dir / "index.sqlite").is_file()
    assert (workspace.state_dir / "index.jsonl").is_file()
    assert lci_index.INDEX_BUILD_LOCK_NAME == "index_build.lock"
    assert (workspace.state_dir / lci_index.INDEX_BUILD_LOCK_NAME).is_file()
    assert lci_index.INDEX_BUILD_TRANSACTION_LOCK_NAME == "index_build.sqlite"
    assert (workspace.state_dir / lci_index.INDEX_BUILD_TRANSACTION_LOCK_NAME).is_file()

    jsonl_rows = [
        json.loads(line)
        for line in (workspace.state_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["id"] for row in jsonl_rows} == {
        "skill:quartz-helper",
        "skill:quartz-router",
    }
    assert all(set(row) == ARTIFACT_RESULT_KEYS for row in jsonl_rows)
    assert all("search_text" not in row for row in jsonl_rows)

    db_path = workspace.state_dir / "index.sqlite"
    rows = indexer.search_index(db_path, "quartz inventory operations", limit=1)
    assert [row["id"] for row in rows] == ["skill:quartz-router"]
    assert set(rows[0]) == SEARCH_RESULT_KEYS
    assert indexer.search_index(db_path, "quartz", artifact_type="script") == []

    fetched = indexer.get_artifact(db_path, "skill:quartz-router")
    assert fetched is not None
    assert fetched["related"] == ["skill:quartz-helper"]
    assert set(fetched) == ARTIFACT_RESULT_KEYS
    assert indexer.get_artifact(db_path, "skill:missing") is None

    neighbors = indexer.get_neighbors(db_path, "skill:quartz-router")
    assert [row["id"] for row in neighbors] == ["skill:quartz-helper"]
    assert neighbors[0]["edge_kind"] == "related_to"
    assert neighbors[0]["edge_evidence"] == "skill:quartz-helper"
    assert set(neighbors[0]) == ARTIFACT_RESULT_KEYS | {"edge_kind", "edge_evidence"}
    assert indexer.get_neighbors(db_path, "skill:missing") == []


def test_cli_core_commands_and_positive_evaluation_output(
    workspace: Workspace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert lci_cli.main(
        [
            "build",
            "--root",
            str(workspace.root),
            "--hermes-home",
            str(workspace.hermes_home),
            "--output-dir",
            str(workspace.state_dir),
        ]
    ) == 0
    build_output = capsys.readouterr().out
    assert "Built 2 artifacts and 1 edges" in build_output
    assert "  skill: 2" in build_output
    assert f"SQLite: {workspace.state_dir / 'index.sqlite'}" in build_output
    assert f"JSONL:  {workspace.state_dir / 'index.jsonl'}" in build_output

    db_path = workspace.state_dir / "index.sqlite"
    assert lci_cli.main(
        ["search", "quartz inventory operations", "--limit", "1", "--db", str(db_path), "--json"]
    ) == 0
    search_rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in search_rows] == ["skill:quartz-router"]
    assert set(search_rows[0]) == SEARCH_RESULT_KEYS

    assert lci_cli.main(["get", "skill:quartz-router", "--db", str(db_path), "--json"]) == 0
    fetched = json.loads(capsys.readouterr().out)
    assert fetched["id"] == "skill:quartz-router"
    assert set(fetched) == ARTIFACT_RESULT_KEYS

    assert lci_cli.main(
        ["neighbors", "skill:quartz-router", "--db", str(db_path), "--json"]
    ) == 0
    neighbors = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in neighbors] == ["skill:quartz-helper"]
    assert set(neighbors[0]) == ARTIFACT_RESULT_KEYS | {"edge_kind", "edge_evidence"}

    assert lci_cli.main(["get", "skill:missing", "--db", str(db_path)]) == 1
    missing = capsys.readouterr()
    assert missing.out == ""
    assert missing.err == "Artifact not found: skill:missing\n"

    feedback = invoke_tool(
        registered_surface(),
        "knowledge_feedback",
        {
            "rating": "useful",
            "query": "quartz inventory operations",
            "artifact_id": "skill:quartz-router",
        },
    )
    assert feedback["success"] is True
    usage_db = workspace.state_dir / "usage.sqlite"

    with sqlite3.connect(usage_db) as conn:
        before = (
            conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
        )

    status, metrics = run_cli_json(
        capsys,
        ["evaluate", "--db", str(db_path), "--usage-db", str(usage_db), "--json"],
    )
    assert status == 0
    assert set(metrics) == EVALUATION_METRIC_KEYS
    assert metrics == {
        "query_count": 1,
        "label_count": 1,
        "hit_at_1": 1.0,
        "hit_at_3": 1.0,
        "hit_at_5": 1.0,
        "hit_at_10": 1.0,
        "mrr_at_10": 1.0,
        "parent_equiv_hit_at_1": 1.0,
        "parent_equiv_hit_at_3": 1.0,
        "parent_equiv_hit_at_5": 1.0,
        "parent_equiv_hit_at_10": 1.0,
        "parent_equiv_mrr_at_10": 1.0,
    }
    assert not any("negative" in key for key in metrics)

    status, details = run_cli_json(
        capsys,
        [
            "evaluate",
            "--db",
            str(db_path),
            "--usage-db",
            str(usage_db),
            "--json",
            "--details",
        ],
    )
    assert status == 0
    assert set(details) == EVALUATION_METRIC_KEYS | {"cases"}
    assert len(details["cases"]) == 1
    assert set(details["cases"][0]) == {
        "query",
        "expected_ids",
        "exact_rank",
        "parent_equiv_rank",
        "top_ids",
    }
    assert details["cases"][0]["expected_ids"] == ["skill:quartz-router"]
    assert details["cases"][0]["exact_rank"] == 1
    assert details["cases"][0]["parent_equiv_rank"] == 1
    assert not any("negative" in key for key in details)

    with sqlite3.connect(usage_db) as conn:
        after = (
            conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
        )
    assert after == before


def test_cli_doctor_and_router_skill_install_status_and_exit_contract(
    workspace: Workspace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, doctor = run_cli_json(
        capsys,
        ["doctor", "--hermes-home", str(workspace.hermes_home), "--json"],
    )
    assert status == 0
    assert doctor["success"] is True
    assert doctor["rebuilt"] is False
    checks = {row["name"]: row for row in doctor["checks"]}
    assert set(checks) >= {
        "hermes_home_exists",
        "source_root_exists",
        "state_dir_parent_exists",
        "index_exists",
        "router_skill_installed",
        "router_skill_matches_plugin",
        "okf_auto_generate",
    }
    assert checks["router_skill_installed"]["ok"] is False
    assert checks["okf_auto_generate"]["ok"] is False

    install_args = [
        "install-router-skill",
        "--hermes-home",
        str(workspace.hermes_home),
        "--json",
    ]
    status, installed = run_cli_json(capsys, install_args)
    target = workspace.hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"
    assert status == 0
    assert installed == {
        "source": str(
            REPO_ROOT
            / "hermes_local_knowledge"
            / "skills"
            / "local-knowledge-router"
            / "SKILL.md"
        ),
        "target": str(target),
        "success": True,
        "status": "installed",
        "overwritten": False,
        "message": "installed normal local-knowledge-router skill",
    }
    assert target.is_file()

    status, current = run_cli_json(capsys, install_args)
    assert status == 0
    assert current["success"] is True
    assert current["status"] == "current"
    assert current["overwritten"] is False

    target.write_text("user-owned router skill\n", encoding="utf-8")
    status, conflict = run_cli_json(capsys, install_args)
    assert status == 1
    assert conflict["success"] is False
    assert conflict["status"] == "conflict"
    assert conflict["force_required"] is True
    assert target.read_text(encoding="utf-8") == "user-owned router skill\n"

    status, overwritten = run_cli_json(capsys, [*install_args, "--force"])
    assert status == 0
    assert overwritten["success"] is True
    assert overwritten["status"] == "installed"
    assert overwritten["overwritten"] is True
    assert target.read_bytes() == Path(overwritten["source"]).read_bytes()


def test_hermes_native_adapter_surface_and_okf_worker_exit_behavior(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = argparse.ArgumentParser(prog="hermes local-knowledge")
    lci_cli.setup_hermes_cli(parser)

    with pytest.raises(SystemExit) as help_exit:
        parser.parse_args(["--help"])
    assert help_exit.value.code == 0
    help_output = capsys.readouterr().out
    assert "install-router-skill" in help_output
    assert "doctor" in help_output
    assert "okf-worker" in help_output

    for argv, snippets in [
        (["install-router-skill", "--help"], ["--hermes-home", "--force", "--json"]),
        (
            ["doctor", "--help"],
            ["--hermes-home", "--rebuild", "--query", "--limit", "--json"],
        ),
        (["okf-worker", "--help"], ["--hermes-home"]),
    ]:
        with pytest.raises(SystemExit) as nested_help_exit:
            parser.parse_args(argv)
        assert nested_help_exit.value.code == 0
        output = capsys.readouterr().out
        assert all(snippet in output for snippet in snippets)

    hermes_home = tmp_path / "adapter-home"
    hermes_home.mkdir()
    write(
        hermes_home / "config.yaml",
        """local_knowledge:
  okf:
    enabled: true
    auto_generate: true
""",
    )
    doctor_args = parser.parse_args(["doctor", "--hermes-home", str(hermes_home), "--json"])
    assert lci_cli.handle_hermes_cli(doctor_args) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["success"] is True
    assert doctor["rebuilt"] is False

    worker_args = parser.parse_args(["okf-worker", "--hermes-home", str(hermes_home)])
    no_model_calls = object()
    assert lci_cli.handle_hermes_cli(worker_args, llm=no_model_calls) == 0
    assert (hermes_home / "local_knowledge" / "okf_queue.sqlite").is_file()

    with pytest.raises(SystemExit) as worker_exit:
        lci_cli.handle_hermes_cli(worker_args, llm=None)
    assert worker_exit.value.code == 2


def test_package_entrypoint_manifest_and_bundled_skill_contract(tmp_path: Path) -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert project["name"] == "hermes-local-knowledge"
    assert project["version"] == hermes_local_knowledge.__version__
    assert project["requires-python"] == ">=3.11"
    assert project.get("dependencies", []) == []
    assert project["entry-points"]["hermes_agent.plugins"] == {
        "local_knowledge": "hermes_local_knowledge.plugin"
    }
    assert pyproject["tool"]["setuptools"]["package-data"] == {
        "hermes_local_knowledge": ["skills/*/SKILL.md"]
    }
    assert callable(plugin.register)

    manifest_lines = (REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()

    def manifest_list(key: str) -> list[str]:
        start = manifest_lines.index(f"{key}:") + 1
        values: list[str] = []
        for line in manifest_lines[start:]:
            if not line.startswith("  - "):
                break
            values.append(line.removeprefix("  - "))
        return values

    assert manifest_list("provides_tools") == EXPECTED_TOOL_ORDER
    assert manifest_list("provides_hooks") == ["post_tool_call", "on_session_finalize"]

    root_skill = REPO_ROOT / "skills" / "local-knowledge-router" / "SKILL.md"
    package_skill = (
        REPO_ROOT
        / "hermes_local_knowledge"
        / "skills"
        / "local-knowledge-router"
        / "SKILL.md"
    )
    example_skill = (
        REPO_ROOT
        / "examples"
        / "local-knowledge-router-skill"
        / "SKILL.md"
    )
    root_bytes = root_skill.read_bytes()
    assert package_skill.read_bytes() == root_bytes
    assert example_skill.read_bytes() == root_bytes
    ctx = registered_surface()
    assert ctx.skills == [("local-knowledge-router", root_skill)]

    expected_sdist_files = {
        "scripts/compare_historical_query_versions.py",
        "scripts/evaluate_ref.py",
        "scripts/evaluation_fixture.py",
        "tests/search_regression_cases.json",
    }
    manifest_entries = {
        line.removeprefix("include ").strip()
        for line in (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        if line.startswith("include ")
    }
    assert expected_sdist_files <= manifest_entries

    source_copy = tmp_path / "source"
    dist_dir = tmp_path / "dist"
    shutil.copytree(
        REPO_ROOT,
        source_copy,
        ignore=shutil.ignore_patterns(
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "*.egg-info",
            "*.pyc",
            "build",
            "dist",
        ),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--sdist",
            "--wheel",
            "--outdir",
            str(dist_dir),
        ],
        cwd=source_copy,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    [sdist_path] = list(dist_dir.glob("*.tar.gz"))
    [wheel_path] = list(dist_dir.glob("*.whl"))

    with tarfile.open(sdist_path, "r:gz") as sdist_archive:
        sdist_names = {
            member.name.split("/", 1)[1]
            for member in sdist_archive.getmembers()
            if "/" in member.name
        }
    assert expected_sdist_files <= sdist_names

    with zipfile.ZipFile(wheel_path) as wheel_archive:
        wheel_names = set(wheel_archive.namelist())
    assert "hermes_local_knowledge/skills/local-knowledge-router/SKILL.md" in wheel_names
    wheel_modules = {
        name.removeprefix("hermes_local_knowledge/")
        for name in wheel_names
        if name.startswith("hermes_local_knowledge/")
        and name.count("/") == 1
        and name.endswith(".py")
    }
    assert wheel_modules == {
        "__init__.py",
        "artifacts.py",
        "cli.py",
        "config.py",
        "evaluation.py",
        "index.py",
        "indexer.py",
        "okf.py",
        "plugin.py",
        "service.py",
        "telemetry.py",
    }
    retired_modules = {
        "constants.py",
        "handlers.py",
        "models.py",
        "paths.py",
        "runtime.py",
        "scanners.py",
        "schemas.py",
        "search.py",
        "storage.py",
        "text_utils.py",
        "tooling.py",
    }
    assert wheel_modules.isdisjoint(retired_modules)


def test_directory_plugin_root_shim_supports_namespaced_import_without_install(
    tmp_path: Path,
) -> None:
    script = textwrap.dedent(
        """
        import importlib.util
        import json
        import pathlib
        import sys
        import types

        root = pathlib.Path(sys.argv[1]).resolve()
        parent_name = "public_contract_plugins"
        parent = types.ModuleType(parent_name)
        parent.__path__ = [str(root.parent)]
        sys.modules[parent_name] = parent
        name = parent_name + ".local_knowledge"
        spec = importlib.util.spec_from_file_location(
            name,
            root / "__init__.py",
            submodule_search_locations=[str(root)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        calls = []
        class Context:
            def register_tool(self, **kwargs):
                calls.append(kwargs["name"])
        module.register(Context())
        implementation = module.register.__module__
        assert implementation == name + ".hermes_local_knowledge.plugin", implementation
        assert calls == [
            "knowledge_search",
            "knowledge_get",
            "knowledge_neighbors",
            "knowledge_feedback",
            "knowledge_usage_report",
        ]
        assert module.__all__ == ["register"]
        print(json.dumps({"implementation": implementation, "tools": calls}, sort_keys=True))
        """
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HOME"] = str(tmp_path / "home")
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script, str(REPO_ROOT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["implementation"].endswith(".hermes_local_knowledge.plugin")
    assert payload["tools"] == EXPECTED_TOOL_ORDER


def test_config_implicit_and_explicit_root_markdown_defaults_via_cli_and_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    implicit_home = tmp_path / "implicit-home"
    implicit_state = tmp_path / "implicit-state"
    explicit_home = tmp_path / "explicit-home"
    explicit_root = tmp_path / "explicit-root"
    explicit_state = tmp_path / "explicit-state"
    implicit_home.mkdir()
    explicit_home.mkdir()
    write(
        implicit_home / "custom_skills" / "implicit-skill" / "SKILL.md",
        "---\nname: implicit-skill\ndescription: Implicit root fixture.\n---\n",
    )
    write(implicit_home / "loose-note.md", "# Implicit loose note\n")
    write(
        explicit_root / "custom_skills" / "explicit-skill" / "SKILL.md",
        "---\nname: explicit-skill\ndescription: Explicit root fixture.\n---\n",
    )
    write(explicit_root / "loose-note.md", "# Explicit loose note\n")
    write(
        implicit_home / "config.yaml",
        f"""local_knowledge:
  state_dir: {implicit_state}
""",
    )
    write(
        explicit_home / "config.yaml",
        f"""local_knowledge:
  source_root: {explicit_root}
  state_dir: {explicit_state}
""",
    )

    def build(hermes_home: Path, state_dir: Path) -> list[dict[str, Any]]:
        assert lci_cli.main(
            ["build", "--hermes-home", str(hermes_home), "--from-hermes-config"]
        ) == 0
        output = capsys.readouterr().out
        assert f"SQLite: {state_dir.resolve() / 'index.sqlite'}" in output
        return [
            json.loads(line)
            for line in (state_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    implicit_rows = build(implicit_home, implicit_state)
    explicit_rows = build(explicit_home, explicit_state)
    assert {row["path"] for row in implicit_rows} == {"custom_skills/implicit-skill"}
    assert {row["path"] for row in explicit_rows} == {
        "custom_skills/explicit-skill",
        "loose-note.md",
    }

    status, implicit = run_cli_json(
        capsys,
        ["doctor", "--hermes-home", str(implicit_home), "--json"],
    )
    assert status == 0
    assert implicit["source_root"] == str(implicit_home.resolve())
    assert implicit["source_root_source"] == "default"
    assert implicit["state_dir"] == str(implicit_state.resolve())
    assert implicit["state_dir_source"] == "config"
    assert implicit["include_markdown_docs_source"] == "default"
    implicit_checks = {row["name"]: row for row in implicit["checks"]}
    assert implicit_checks["okf_auto_generate"]["ok"] is False

    status, explicit = run_cli_json(
        capsys,
        ["doctor", "--hermes-home", str(explicit_home), "--json"],
    )
    assert status == 0
    assert explicit["source_root"] == str(explicit_root.resolve())
    assert explicit["source_root_source"] == "config"
    assert explicit["state_dir"] == str(explicit_state.resolve())
    assert explicit["state_dir_source"] == "config"
    assert explicit["include_markdown_docs_source"] == "default"

    monkeypatch.setenv("HERMES_HOME", str(implicit_home))
    hook = registered_surface().hooks["post_tool_call"]
    hook(
        tool_name="public_default_tool",
        args={"query": "public fixture"},
        result=json.dumps({"success": True}),
    )
    status, queue = run_cli_json(
        capsys,
        [
            "okf",
            "status",
            "--hermes-home",
            str(implicit_home),
            "--from-hermes-config",
            "--json",
        ],
    )
    assert status == 0
    assert [row["tool"] for row in queue["pending"]] == ["public_default_tool"]


def test_config_legacy_aliases_match_canonical_scanner_behavior_via_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "source"
    canonical_home = tmp_path / "canonical-home"
    legacy_home = tmp_path / "legacy-home"
    canonical_state = tmp_path / "canonical-state"
    legacy_state = tmp_path / "legacy-state"
    canonical_home.mkdir()
    legacy_home.mkdir()
    write(
        source_root / "alpha" / "alpha-skill" / "SKILL.md",
        "---\nname: alpha-skill\ndescription: Quartz and Acme alpha skill.\n---\n",
    )
    write(
        source_root / "beta" / "beta-skill" / "SKILL.md",
        "---\nname: beta-skill\ndescription: Quartz and Acme beta skill.\n---\n",
    )
    write(
        source_root / "scripts_one" / "quartz_tool.py",
        "# Maintain Quartz and Acme inventory records.\n",
    )
    write(
        source_root / "scripts_two" / "acme_tool.sh",
        "#!/bin/sh\n# Maintain Quartz and Acme inventory records.\n",
    )
    write(
        source_root / "scripts_one" / "build" / "configured-exclusion.py",
        "# Configured exclusion.\n",
    )
    write(
        source_root / "scripts_one" / ".archive" / "builtin-exclusion.py",
        "# Built-in exclusion.\n",
    )
    write(source_root / "memory_one" / "quartz-memory.md", "# Quartz and Acme memory\n")
    write(source_root / "memory_two" / "acme-memory.md", "# Acme and Quartz memory\n")
    write(source_root / "docs_one" / "quartz-runbook.md", "# Quartz and Acme runbook\n")
    write(source_root / "docs_two" / "acme-runbook.md", "# Acme and Quartz runbook\n")
    write(source_root / "loose-note.md", "# Quartz and Acme loose note\n")

    scanner_config = "  exclude_dir_names: [build, dist]\n"
    write(
        canonical_home / "config.yaml",
        f"""local_knowledge:
  source_root: {source_root}
  state_dir: {canonical_state}
  custom_skill_dirs: [alpha, beta]
  script_dirs: [scripts_one, scripts_two]
  memory_dirs: [memory_one, memory_two]
  runbook_dirs: [docs_one, docs_two]
  known_entities: [Quartz, Acme]
{scanner_config}""",
    )
    write(
        legacy_home / "config.yaml",
        f"""local_knowledge:
  root: {source_root}
  index_dir: {legacy_state}
  custom_skill_dirs: 'alpha, beta'
  script_dirs: '[scripts_one, scripts_two]'
  memory_dirs: 'memory_one, memory_two'
  runbook_dirs: '[docs_one, docs_two]'
  entities: '[Quartz, Acme]'
{scanner_config}""",
    )

    def build(hermes_home: Path, state_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        assert lci_cli.main(
            ["build", "--hermes-home", str(hermes_home), "--from-hermes-config"]
        ) == 0
        output = capsys.readouterr().out
        assert f"SQLite: {state_dir.resolve() / 'index.sqlite'}" in output
        rows = [
            json.loads(line)
            for line in (state_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        status, doctor = run_cli_json(
            capsys,
            ["doctor", "--hermes-home", str(hermes_home), "--json"],
        )
        assert status == 0
        return rows, doctor

    canonical_rows, canonical = build(canonical_home, canonical_state)
    legacy_rows, legacy = build(legacy_home, legacy_state)
    assert legacy_rows == canonical_rows
    paths = {row["path"] for row in canonical_rows}
    assert paths == {
        "alpha/alpha-skill",
        "beta/beta-skill",
        "scripts_one/quartz_tool.py",
        "scripts_two/acme_tool.sh",
        "memory_one/quartz-memory.md",
        "memory_two/acme-memory.md",
        "docs_one/quartz-runbook.md",
        "docs_two/acme-runbook.md",
        "loose-note.md",
    }
    assert {row["type"] for row in canonical_rows} == {
        "doc",
        "memory_doc",
        "runbook",
        "script",
        "skill",
    }
    quartz_script = next(
        row for row in canonical_rows if row["path"] == "scripts_one/quartz_tool.py"
    )
    assert quartz_script["entities"] == ["Quartz", "Acme"]
    assert "scripts_one/build/configured-exclusion.py" not in paths
    assert "scripts_one/.archive/builtin-exclusion.py" not in paths

    for payload, hermes_home, state_dir in [
        (canonical, canonical_home, canonical_state),
        (legacy, legacy_home, legacy_state),
    ]:
        assert payload["hermes_home"] == str(hermes_home.resolve())
        assert payload["source_root"] == str(source_root.resolve())
        assert payload["source_root_source"] == "config"
        assert payload["state_dir"] == str(state_dir.resolve())
        assert payload["state_dir_source"] == "config"
        assert payload["include_markdown_docs_source"] == "default"
        assert payload["warnings"] == []


def test_config_hermes_home_config_and_env_precedence_via_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hermes_home = tmp_path / "selected-hermes-home"
    config_root = tmp_path / "config-root"
    config_state = tmp_path / "config-state"
    env_root = tmp_path / "env-root"
    env_state = tmp_path / "env-state"
    for path in (hermes_home, config_root, env_root):
        path.mkdir()
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {config_root}
  state_dir: {config_state}
""",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    status, configured = run_cli_json(capsys, ["doctor", "--json"])
    assert status == 0
    assert configured["hermes_home"] == str(hermes_home.resolve())
    assert configured["source_root"] == str(config_root.resolve())
    assert configured["state_dir"] == str(config_state.resolve())
    assert configured["source_root_source"] == "config"
    assert configured["state_dir_source"] == "config"

    status, smoke = run_cli_json(capsys, ["smoke", "--json"])
    assert status == 0
    assert smoke["source_root"] == configured["source_root"]
    assert smoke["state_dir"] == configured["state_dir"]

    monkeypatch.setenv("LOCAL_KNOWLEDGE_ROOT", str(env_root))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_STATE_DIR", str(env_state))
    status, overridden = run_cli_json(capsys, ["doctor", "--json"])
    assert status == 0
    assert overridden["hermes_home"] == str(hermes_home.resolve())
    assert overridden["source_root"] == str(env_root.resolve())
    assert overridden["state_dir"] == str(env_state.resolve())
    assert overridden["source_root_source"] == "env"
    assert overridden["state_dir_source"] == "env"

    cli_root = tmp_path / "cli-root"
    cli_state = tmp_path / "cli-state"
    write(
        cli_root / "custom_skills" / "cli-skill" / "SKILL.md",
        "---\nname: cli-skill\ndescription: CLI precedence fixture.\n---\n",
    )
    assert lci_cli.main(
        [
            "build",
            "--root",
            str(cli_root),
            "--output-dir",
            str(cli_state),
            "--hermes-home",
            str(hermes_home),
        ]
    ) == 0
    build_output = capsys.readouterr().out
    assert f"SQLite: {cli_state.resolve() / 'index.sqlite'}" in build_output
    rows = [
        json.loads(line)
        for line in (cli_state / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["id"] for row in rows] == ["skill:cli-skill"]


@pytest.mark.parametrize("has_hermes_agent", [False, True], ids=["absent", "present"])
def test_implicit_hermes_home_warning_is_conditional(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    has_hermes_agent: bool,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    if has_hermes_agent:
        (hermes_home / "hermes-agent").mkdir()

    status, payload = run_cli_json(
        capsys,
        ["doctor", "--hermes-home", str(hermes_home), "--json"],
    )
    assert status == 0
    assert payload["source_root_source"] == "default"
    if has_hermes_agent:
        assert len(payload["warnings"]) == 1
        assert "Because HERMES_HOME/hermes-agent exists" in payload["warnings"][0]
    else:
        assert payload["warnings"] == []


@pytest.mark.parametrize(
    ("config_body", "candidates", "expected_batch_size", "expected_timeout"),
    [
        (
            """  okf:
    auto_generate: true
""",
            [("alpha_tool", 1), ("beta_tool", 1), ("gamma_tool", 1)],
            2,
            120,
        ),
        (
            """  okf_enabled: false
  okf_auto_generate: true
""",
            [("disabled_tool", 1)],
            0,
            None,
        ),
        (
            """  okf_enabled: true
  okf_auto_generate: true
  okf_max_candidates_per_session: 1
  okf_max_worker_seconds: 333
  okf_min_use_count: 2
""",
            [("low_use_tool", 1), ("high_use_tool", 2), ("highest_use_tool", 3)],
            1,
            333,
        ),
        (
            """  okf_enabled: true
  okf_auto_generate: true
  okf_max_generation_seconds: 0
  okf_max_worker_seconds: 333
""",
            [("generation_timeout_tool", 1)],
            1,
            10,
        ),
        (
            """  okf_enabled: false
  okf_auto_generate: false
  okf_max_candidates_per_session: 9
  okf_max_generation_seconds: 180
  okf_min_use_count: 1
  okf:
    enabled: true
    auto_generate: true
    max_candidates_per_session: 1
    max_generation_seconds: 240
    max_worker_seconds: 999
    min_use_count: 3
""",
            [("below_nested_minimum", 2), ("nested_selected_tool", 3)],
            1,
            240,
        ),
    ],
)
def test_config_nested_and_flat_okf_settings_through_native_worker(
    tmp_path: Path,
    config_body: str,
    candidates: list[tuple[str, int]],
    expected_batch_size: int,
    expected_timeout: int | None,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    source_root = tmp_path / "source"
    state_dir = tmp_path / "state"
    hermes_home.mkdir()
    source_root.mkdir()
    create_current_okf_queue(state_dir, candidates=candidates)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {source_root}
  state_dir: {state_dir}
{config_body}""",
    )

    class CapturingLLM:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def complete_structured(self, **kwargs: Any) -> SimpleNamespace:
            self.calls.append(kwargs)
            input_items = cast(list[dict[str, str]], kwargs["input"])
            packet = json.loads(input_items[0]["text"])
            okfs = []
            for candidate in packet["candidates"]:
                readable_tool = str(candidate["tool"]).replace("_", " ")
                okfs.append(
                    {
                        "tool": candidate["tool"],
                        "schema_hash": candidate["schema_hash"],
                        "title": f"{readable_tool.title()} router",
                        "aliases": [f"route {readable_tool} requests"],
                        "triggers": [f"User asks to route {readable_tool} requests."],
                        "when_not_to_use": [],
                        "related_tools": [],
                        "body": f"Route {readable_tool} requests using the supplied query input.",
                    }
                )
            return SimpleNamespace(parsed={"okfs": okfs})

    llm = CapturingLLM()
    ctx = registered_surface(llm=llm)
    registration = ctx.cli_commands[0]
    parser = argparse.ArgumentParser(prog="hermes local-knowledge")
    setup_fn = cast(Callable[[argparse.ArgumentParser], None], registration["setup_fn"])
    handler_fn = cast(Callable[[argparse.Namespace], int], registration["handler_fn"])
    setup_fn(parser)
    args = parser.parse_args(["okf-worker", "--hermes-home", str(hermes_home)])
    assert handler_fn(args) == 0
    assert len(llm.calls) == int(expected_batch_size > 0)
    if expected_batch_size == 0:
        return
    call = llm.calls[0]
    input_items = cast(list[dict[str, str]], call["input"])
    packet = json.loads(input_items[0]["text"])
    assert len(packet["candidates"]) == expected_batch_size
    assert call["timeout"] == expected_timeout


def test_usage_sqlite_schema_report_rows_and_public_keys(
    workspace: Workspace,
) -> None:
    usage_db = create_current_usage_history(workspace.state_dir, workspace.root)
    event_columns = ", ".join(row[0] for row in USAGE_EVENT_SCHEMA)
    feedback_columns = ", ".join(row[0] for row in FEEDBACK_SCHEMA)
    with sqlite3.connect(usage_db) as conn:
        seeded_event = conn.execute(
            f"SELECT {event_columns} FROM usage_events WHERE id = 4001"
        ).fetchone()
        seeded_feedback = conn.execute(
            f"SELECT {feedback_columns} FROM feedback WHERE id = 5001"
        ).fetchone()
    assert seeded_event is not None
    assert seeded_feedback is not None
    ctx = registered_surface()
    search = invoke_tool(
        ctx,
        "knowledge_search",
        {"query": "quartz inventory operations", "rebuild": True},
    )
    invoke_tool(ctx, "knowledge_get", {"artifact_id": "skill:quartz-router"})
    invoke_tool(ctx, "knowledge_neighbors", {"artifact_id": "skill:quartz-router"})
    invoke_tool(
        ctx,
        "knowledge_feedback",
        {
            "rating": "useful",
            "event_id": search["usage_event_id"],
            "query": "quartz inventory operations",
            "artifact_id": "skill:quartz-router",
        },
    )
    report = invoke_tool(ctx, "knowledge_usage_report", {"days": 30, "limit": 10})

    assert usage_db.is_file()
    with sqlite3.connect(usage_db) as conn:
        assert table_columns(conn, "usage_events") >= {row[0] for row in USAGE_EVENT_SCHEMA}
        assert table_columns(conn, "feedback") >= {row[0] for row in FEEDBACK_SCHEMA}
        assert conn.execute(
            f"SELECT {event_columns} FROM usage_events WHERE id = 4001"
        ).fetchone() == seeded_event
        assert conn.execute(
            f"SELECT {feedback_columns} FROM feedback WHERE id = 5001"
        ).fetchone() == seeded_feedback

    assert set(report) == USAGE_REPORT_KEYS
    assert report["total_events"] >= 4
    assert report["feedback_count"] == 2
    assert any(row["query"] == "seeded historical quartz" for row in report["top_queries"])
    assert any(
        row["rating"] == "useful" and row["count"] == 2
        for row in report["feedback_by_rating"]
    )
    assert set(report["root_breakdown"][0]) == {
        "root_scope",
        "count",
        "successes",
        "errors",
        "last_seen",
    }
    assert set(report["feedback_root_breakdown"][0]) == {"root_scope", "count", "last_seen"}
    assert set(report["top_tools"][0]) == {
        "client",
        "tool",
        "count",
        "successes",
        "errors",
        "avg_latency_ms",
    }
    assert set(report["top_queries"][0]) == {"query", "count", "avg_results", "last_seen"}
    assert set(report["top_artifacts"][0]) == {"artifact_id", "count", "last_seen"}
    assert set(report["feedback_by_rating"][0]) == {"rating", "count", "last_seen"}
    assert set(report["feedback_rating_buckets"][0]) == {"rating", "count", "last_seen"}
    assert set(report["latest_index_metadata"]) == {
        "id",
        "ts",
        "client",
        "tool",
        "plugin_version",
        "root",
        "db_path",
        "source_root_source",
        "state_dir_source",
        "include_markdown_docs_source",
        "index_exists",
        "index_mtime",
        "index_age_seconds",
        "index_artifact_count",
        "index_edge_count",
        "index_metadata_error",
        "build_duration_ms",
        "rebuilt",
        "index_artifact_counts",
    }
    assert set(report["recent_builds"][0]) == {
        "id",
        "ts",
        "client",
        "tool",
        "plugin_version",
        "root",
        "db_path",
        "source_root_source",
        "state_dir_source",
        "index_mtime",
        "index_artifact_count",
        "index_edge_count",
        "build_duration_ms",
        "rebuilt",
        "index_artifact_counts",
    }


def test_okf_cli_reads_current_queue_and_preserves_command_exit_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "state"
    db_path = create_current_okf_queue(state_dir)
    common = ["--state-dir", str(state_dir), "--json"]

    status, payload = run_cli_json(capsys, ["okf", "status", *common])
    assert status == 0
    assert set(payload) == {
        "success",
        "state_dir",
        "okf_dir",
        "queue_db",
        "counts",
        "pending",
        "errors",
    }
    assert payload["success"] is True
    assert payload["queue_db"] == str(db_path.resolve())
    assert payload["okf_dir"] == str(state_dir.resolve() / "okfs" / "tools")
    assert payload["counts"] == {"pending": 1}
    assert payload["errors"] == []
    candidate = payload["pending"][0]
    assert set(candidate) == {
        "tool",
        "tool_name",
        "toolset",
        "schema_hash",
        "schema",
        "generator_version",
        "allowed_related_tools",
        "arg_shape",
        "use_count",
        "success_count",
        "error_count",
        "last_error_type",
        "last_error_message",
        "claim_token",
        "target_path",
    }
    assert candidate["tool"] == "public_demo_tool"
    assert candidate["tool_name"] == "public_demo_tool"
    assert candidate["toolset"] == "local_knowledge"
    assert candidate["generator_version"] == "3"
    assert candidate["allowed_related_tools"] == []
    assert candidate["target_path"] == str(
        state_dir.resolve() / "okfs" / "tools" / "public-demo-tool.md"
    )
    for attempt in range(1, 4):
        token = f"claim-{attempt}"
        claim_status, claim = run_cli_json(
            capsys,
            ["okf", "claim", "--claim-token", token, *common],
        )
        assert claim_status == 0
        assert set(claim) == {
            "success",
            "state_dir",
            "claim_token",
            "count",
            "candidates",
        }
        assert claim["success"] is True
        assert claim["claim_token"] == token
        assert claim["count"] == 1
        fail_status, failed = run_cli_json(
            capsys,
            [
                "okf",
                "fail",
                "--claim-token",
                token,
                "--tool",
                "public_demo_tool",
                "--error",
                "public fixture generation failed",
                *common,
            ],
        )
        assert fail_status == 0
        assert failed == {
            "success": True,
            "tool": "public_demo_tool",
            "claim_token": token,
        }

    status, exhausted = run_cli_json(capsys, ["okf", "status", *common])
    assert status == 0
    assert exhausted["counts"] == {"error": 1}
    assert exhausted["pending"] == []
    assert [row["tool"] for row in exhausted["errors"]] == ["public_demo_tool"]

    retry_status, retried = run_cli_json(
        capsys,
        ["okf", "retry", "--tool", "public_demo_tool", *common],
    )
    assert retry_status == 0
    assert retried == {"success": True, "tool": "public_demo_tool"}

    claim_status, claim = run_cli_json(
        capsys,
        ["okf", "claim", "--claim-token", "claim-final", *common],
    )
    assert claim_status == 0
    target = Path(claim["candidates"][0]["target_path"])

    invalid_status, invalid = run_cli_json(
        capsys,
        [
            "okf",
            "validate",
            "--claim-token",
            "claim-final",
            "--path",
            str(target),
            *common,
        ],
    )
    assert invalid_status == 1
    assert invalid["success"] is False
    assert invalid["valid"] is False
    assert "OKF file is missing or empty" in invalid["errors"]

    write(
        target,
        """---
artifact_type: tool_okf
tool: public_demo_tool
toolset: local_knowledge
schema_hash: sha256:public-fixture
generator_version: "3"
title: Public demo router
aliases:
  - route quartz inventory records
triggers:
  - User asks for the public demo routing capability.
when_not_to_use:
related_tools:
---
# Public demo router

Route public demo requests to the quartz inventory capability.
""",
    )
    validate_status, validation = run_cli_json(
        capsys,
        [
            "okf",
            "validate",
            "--claim-token",
            "claim-final",
            "--path",
            str(target),
            *common,
        ],
    )
    assert validate_status == 0
    assert validation == {
        "valid": True,
        "errors": [],
        "tool": "public_demo_tool",
        "path": str(target.resolve()),
        "claim_token": "claim-final",
        "success": True,
    }

    complete_status, completed = run_cli_json(
        capsys,
        [
            "okf",
            "complete",
            "--claim-token",
            "claim-final",
            "--tool",
            "public_demo_tool",
            "--path",
            str(target),
            *common,
        ],
    )
    assert complete_status == 0
    assert completed == {
        "success": True,
        "tool": "public_demo_tool",
        "path": str(target),
        "claim_token": "claim-final",
    }
    assert target == state_dir.resolve() / "okfs" / "tools" / "public-demo-tool.md"
    assert target.is_file()
    assert (state_dir / "okf_queue.sqlite").is_file()
    assert (state_dir / "okf_index_dirty").is_dir()
    assert len(list((state_dir / "okf_index_dirty").iterdir())) == 1
    status, final_status = run_cli_json(capsys, ["okf", "status", *common])
    assert status == 0
    assert final_status["counts"] == {"done": 1}
    assert final_status["pending"] == []
    assert final_status["errors"] == []


def test_registered_finalize_hook_uses_public_okf_worker_log_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    hermes_home = tmp_path / "hermes-home"
    state_dir = tmp_path / "state"
    root.mkdir()
    hermes_home.mkdir()
    create_current_okf_queue(state_dir)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {state_dir}
  okf:
    enabled: true
    auto_generate: true
    min_use_count: 1
""",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_ROOT", str(root))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_STATE_DIR", str(state_dir))

    calls: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProcess:
        pid = 4242

        def wait(self) -> int:
            return 0

    def fake_popen(command: Sequence[str], **kwargs: Any) -> FakeProcess:
        calls.append((list(command), kwargs))
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ctx = registered_surface()
    assert ctx.hooks["on_session_finalize"](session_id="public-contract") is True
    assert len(calls) == 1
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
    env = cast(dict[str, str], kwargs["env"])
    assert env["HERMES_LOCAL_KNOWLEDGE_OKF_WORKER"] == "1"
    assert env["HERMES_HOME"] == str(hermes_home.resolve())
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["close_fds"] is True
    if os.name == "nt":
        expected_flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP"))
        expected_flags |= int(getattr(subprocess, "DETACHED_PROCESS"))
        assert kwargs["creationflags"] & expected_flags == expected_flags
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True
        assert "creationflags" not in kwargs
    log_handle = cast(TextIO, kwargs["stdout"])
    assert Path(log_handle.name) == state_dir.resolve() / "okf_worker.log"
    assert log_handle.closed is True
    assert (state_dir / "okf_worker.log").is_file()


def test_module_and_direct_indexer_execution_entrypoints(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HOME"] = str(tmp_path / "home")
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    commands = [
        ([sys.executable, "-m", "hermes_local_knowledge.cli", "--help"], REPO_ROOT),
        ([sys.executable, "-m", "hermes_local_knowledge.indexer", "--help"], REPO_ROOT),
        ([sys.executable, str(REPO_ROOT / "hermes_local_knowledge" / "indexer.py"), "--help"], tmp_path),
    ]
    for command, cwd in commands:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "build index.sqlite and index.jsonl" in result.stdout
        assert "search artifacts" in result.stdout
        assert "install-router-skill" in result.stdout
        assert "manage generated tool OKF candidate queue" in result.stdout
