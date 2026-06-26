# pyright: reportMissingImports=false
"""Hermes plugin exposing a local capability index as native tools."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from . import indexer

try:  # Hermes runtime path
    from tools.registry import tool_error, tool_result
except Exception:  # pragma: no cover - lets direct tests run outside Hermes

    def tool_error(message, **extra) -> str:  # type: ignore[no-redef]
        payload = {"error": str(message)}
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    def tool_result(data=None, **kwargs) -> str:  # type: ignore[no-redef]
        return json.dumps(data if data is not None else kwargs, ensure_ascii=False)


ROOT_ENV = "LOCAL_KNOWLEDGE_ROOT"
STATE_ENV = "LOCAL_KNOWLEDGE_STATE_DIR"
CONFIG_SECTION = "local_knowledge"
TOOLSET = "local_knowledge"

FEEDBACK_RATINGS = {
    "useful",
    "not_useful",
    "missing",
    "noisy",
    "wrong_artifact",
    "stale",
    "other",
}
NEGATIVE_FEEDBACK_RATINGS = FEEDBACK_RATINGS - {"useful", "other"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

KNOWLEDGE_SEARCH_SCHEMA: dict[str, Any] = {
    "name": "knowledge_search",
    "description": (
        "Search a local capability index to find the right local skill, "
        "script, runbook, cron job, MCP wrapper, or service doc to inspect first. "
        "Use this before broad file search for Hermes-local, homelab, Paperless, "
        "Docker, SiYuan/wiki, cron, MCP, or service-operation questions. Builds "
        "the index automatically when missing. Usage is logged locally for "
        "closed-loop router improvement."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query, e.g. 'paperless review' or 'siyuan mcp'.",
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
                "description": "Force a rebuild of the configured state_dir/index.sqlite before searching. Default false.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

KNOWLEDGE_GET_SCHEMA: dict[str, Any] = {
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
                "description": "Artifact id such as skill:paperless-review-automation or cron:paperless-reviewer.",
            },
            "include_neighbors": {
                "type": "boolean",
                "description": "Also include graph neighbors for this artifact. Default false.",
            },
            "rebuild": {
                "type": "boolean",
                "description": "Force a rebuild of the configured state_dir/index.sqlite before reading. Default false.",
            },
        },
        "required": ["artifact_id"],
        "additionalProperties": False,
    },
}

KNOWLEDGE_NEIGHBORS_SCHEMA: dict[str, Any] = {
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
                "description": "Force a rebuild of the configured state_dir/index.sqlite before reading. Default false.",
            },
        },
        "required": ["artifact_id"],
        "additionalProperties": False,
    },
}

KNOWLEDGE_FEEDBACK_SCHEMA: dict[str, Any] = {
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
                "enum": sorted(FEEDBACK_RATINGS),
                "description": "Feedback rating: useful, not_useful, missing, noisy, wrong_artifact, stale, or other.",
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
                "description": "Short concrete note about what worked or what should improve. Do not include secrets.",
            },
        },
        "required": ["rating"],
        "additionalProperties": False,
    },
}

KNOWLEDGE_USAGE_REPORT_SCHEMA: dict[str, Any] = {
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
}


# ---------------------------------------------------------------------------
# Local index plumbing
# ---------------------------------------------------------------------------

def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class RuntimeConfig:
    source_root: Path
    hermes_home: Path
    state_dir: Path
    index_settings: indexer.IndexSettings


def _get_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _load_hermes_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config  # type: ignore

        config = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:
        config = indexer.load_yaml_if_available(_get_hermes_home() / "config.yaml")
        return config if isinstance(config, dict) else {}


def _section_config() -> dict[str, Any]:
    section = _load_hermes_config().get(CONFIG_SECTION, {})
    return section if isinstance(section, dict) else {}


def _config_value(*keys: str, default: Any = None) -> Any:
    section = _section_config()
    for key in keys:
        if key in section and section[key] not in (None, ""):
            return section[key]
    return default


def _path_value(value: Any, default: Path) -> Path:
    if value in (None, ""):
        return default.expanduser()
    return Path(str(value)).expanduser()


def _tuple_value(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip()) or default
    if isinstance(value, (list, tuple)):
        items = tuple(str(item).strip() for item in value if str(item).strip())
        return items or default
    return default


def _runtime_config() -> RuntimeConfig:
    hermes_home = _path_value(_config_value("hermes_home"), _get_hermes_home()).resolve()
    env_root = os.environ.get(ROOT_ENV)
    source_root = _path_value(
        env_root or _config_value("source_root", "root"),
        hermes_home,
    ).resolve()
    state_dir = _path_value(
        os.environ.get(STATE_ENV) or _config_value("state_dir", "index_dir"),
        hermes_home / "local_knowledge",
    ).resolve()

    defaults = indexer.IndexSettings()
    known_entities = _tuple_value(_config_value("known_entities", "entities"), defaults.known_entities)
    settings = indexer.IndexSettings(
        custom_skill_dirs=_tuple_value(_config_value("custom_skill_dirs"), defaults.custom_skill_dirs),
        script_dirs=_tuple_value(_config_value("script_dirs"), defaults.script_dirs),
        memory_dirs=_tuple_value(_config_value("memory_dirs"), defaults.memory_dirs),
        runbook_dirs=_tuple_value(_config_value("runbook_dirs"), defaults.runbook_dirs),
        known_entities=known_entities,
    )
    return RuntimeConfig(source_root, hermes_home, state_dir, settings)


def _repo_root() -> Path:
    return _runtime_config().source_root


def _index_module(root: Path):
    return indexer


def _output_dir(root: Path) -> Path:
    return _runtime_config().state_dir


def _db_path(root: Path) -> Path:
    return _output_dir(root) / "index.sqlite"


def _usage_db_path(root: Path) -> Path:
    return _output_dir(root) / "usage.sqlite"


def _ensure_index(root: Path, *, rebuild: bool = False) -> tuple[Path, dict[str, Any]]:
    cfg = _runtime_config()
    db_path = cfg.state_dir / "index.sqlite"
    metadata: dict[str, Any] = {
        "root": str(cfg.source_root),
        "state_dir": str(cfg.state_dir),
        "db_path": str(db_path),
        "rebuilt": False,
    }
    if rebuild or not db_path.exists():
        artifacts, edges = indexer.build_index(
            cfg.source_root,
            cfg.state_dir,
            cfg.hermes_home,
            cfg.index_settings,
        )
        metadata.update(
            {
                "rebuilt": True,
                "artifact_count": len(artifacts),
                "edge_count": len(edges),
            }
        )
    return db_path, metadata


def check_knowledge_available() -> bool:
    try:
        cfg = _runtime_config()
        return cfg.source_root.exists() and cfg.hermes_home.exists()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: Any, *, limit: int = 1000) -> str:
    text = str(value or "")
    text = " ".join(text.replace("\x00", "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _json_list(values: list[str] | None) -> str:
    return json.dumps(values or [], ensure_ascii=False)


USAGE_EVENT_COLUMNS: dict[str, str] = {
    "session_id": "TEXT",
    "task_id": "TEXT",
    "tool_call_id": "TEXT",
    "query": "TEXT",
    "artifact_id": "TEXT",
    "artifact_type": "TEXT",
    "limit_value": "INTEGER",
    "rebuild_requested": "INTEGER NOT NULL DEFAULT 0",
    "rebuilt": "INTEGER",
    "success": "INTEGER NOT NULL DEFAULT 1",
    "error": "TEXT",
    "result_count": "INTEGER",
    "top_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    "top_types_json": "TEXT NOT NULL DEFAULT '[]'",
    "latency_ms": "INTEGER",
    "root": "TEXT",
    "db_path": "TEXT",
}

FEEDBACK_COLUMNS: dict[str, str] = {
    "event_id": "INTEGER",
    "query": "TEXT",
    "artifact_id": "TEXT",
    "note": "TEXT",
    "session_id": "TEXT",
    "task_id": "TEXT",
    "tool_call_id": "TEXT",
    "root": "TEXT",
}


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _usage_context(kwargs: dict[str, Any]) -> dict[str, str]:
    return {
        "session_id": _clean_text(kwargs.get("session_id"), limit=128),
        "task_id": _clean_text(kwargs.get("task_id"), limit=128),
        "tool_call_id": _clean_text(kwargs.get("tool_call_id"), limit=128),
    }


def _init_usage_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            tool TEXT NOT NULL,
            session_id TEXT,
            task_id TEXT,
            tool_call_id TEXT,
            query TEXT,
            artifact_id TEXT,
            artifact_type TEXT,
            limit_value INTEGER,
            rebuild_requested INTEGER NOT NULL DEFAULT 0,
            rebuilt INTEGER,
            success INTEGER NOT NULL,
            error TEXT,
            result_count INTEGER,
            top_ids_json TEXT NOT NULL DEFAULT '[]',
            top_types_json TEXT NOT NULL DEFAULT '[]',
            latency_ms INTEGER,
            root TEXT,
            db_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event_id INTEGER,
            rating TEXT NOT NULL,
            query TEXT,
            artifact_id TEXT,
            note TEXT,
            session_id TEXT,
            task_id TEXT,
            tool_call_id TEXT,
            root TEXT
        )
        """
    )
    _ensure_columns(conn, "usage_events", USAGE_EVENT_COLUMNS)
    _ensure_columns(conn, "feedback", FEEDBACK_COLUMNS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_tool ON usage_events(tool)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_query ON usage_events(query)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_rating ON feedback(rating)")


def _usage_connect(root: Path) -> sqlite3.Connection:
    usage_db = _usage_db_path(root)
    usage_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(usage_db))
    conn.row_factory = sqlite3.Row
    _init_usage_db(conn)
    return conn


def _record_usage(
    root: Path | None,
    *,
    tool: str,
    success: bool,
    query: str = "",
    artifact_id: str = "",
    artifact_type: str = "",
    limit_value: int | None = None,
    rebuild_requested: bool = False,
    rebuilt: bool | None = None,
    error: str = "",
    result_count: int | None = None,
    top_ids: list[str] | None = None,
    top_types: list[str] | None = None,
    latency_ms: int | None = None,
    db_path: Path | None = None,
    context: dict[str, str] | None = None,
) -> int | None:
    if root is None:
        return None
    try:
        context = context or {}
        conn = _usage_connect(root)
        try:
            cur = conn.execute(
                """
                INSERT INTO usage_events (
                    ts, tool, session_id, task_id, tool_call_id, query,
                    artifact_id, artifact_type, limit_value, rebuild_requested,
                    rebuilt, success, error, result_count, top_ids_json,
                    top_types_json, latency_ms, root, db_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    tool,
                    context.get("session_id") or None,
                    context.get("task_id") or None,
                    context.get("tool_call_id") or None,
                    _clean_text(query, limit=1000) or None,
                    _clean_text(artifact_id, limit=300) or None,
                    _clean_text(artifact_type, limit=80) or None,
                    limit_value,
                    1 if rebuild_requested else 0,
                    None if rebuilt is None else (1 if rebuilt else 0),
                    1 if success else 0,
                    _clean_text(error, limit=1000) or None,
                    result_count,
                    _json_list(top_ids),
                    _json_list(top_types),
                    latency_ms,
                    str(root),
                    str(db_path) if db_path else None,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()
    except Exception:
        # Telemetry must never break the lookup tools.
        return None


def _record_feedback(
    root: Path,
    *,
    rating: str,
    event_id: int | None,
    query: str,
    artifact_id: str,
    note: str,
    context: dict[str, str],
) -> int:
    conn = _usage_connect(root)
    try:
        cur = conn.execute(
            """
            INSERT INTO feedback (
                ts, event_id, rating, query, artifact_id, note,
                session_id, task_id, tool_call_id, root
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                event_id,
                rating,
                _clean_text(query, limit=1000) or None,
                _clean_text(artifact_id, limit=300) or None,
                _clean_text(note, limit=2000) or None,
                context.get("session_id") or None,
                context.get("task_id") or None,
                context.get("tool_call_id") or None,
                str(root),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _usage_report(root: Path, *, days: int, limit: int) -> dict[str, Any]:
    usage_db = _usage_db_path(root)
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    since = since_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    if not usage_db.exists():
        return {
            "success": True,
            "usage_db_path": str(usage_db),
            "since": since,
            "days": days,
            "total_events": 0,
            "feedback_count": 0,
            "top_tools": [],
            "top_queries": [],
            "zero_result_queries": [],
            "top_artifacts": [],
            "errors": [],
            "feedback_by_rating": [],
            "recent_negative_feedback": [],
            "improvement_candidates": [],
        }

    conn = _usage_connect(root)
    try:
        total_events = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE ts >= ?",
            (since,),
        ).fetchone()[0]
        feedback_count = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE ts >= ?",
            (since,),
        ).fetchone()[0]
        avg_latency = conn.execute(
            "SELECT AVG(latency_ms) FROM usage_events WHERE ts >= ? AND latency_ms IS NOT NULL",
            (since,),
        ).fetchone()[0]
        top_tools = _rows(
            conn,
            """
            SELECT tool, COUNT(*) AS count, SUM(success) AS successes,
                   COUNT(*) - SUM(success) AS errors,
                   ROUND(AVG(latency_ms), 1) AS avg_latency_ms
            FROM usage_events
            WHERE ts >= ?
            GROUP BY tool
            ORDER BY count DESC, tool
            LIMIT ?
            """,
            (since, limit),
        )
        top_queries = _rows(
            conn,
            """
            SELECT query, COUNT(*) AS count, ROUND(AVG(result_count), 1) AS avg_results,
                   MAX(ts) AS last_seen
            FROM usage_events
            WHERE ts >= ? AND tool = 'knowledge_search' AND query IS NOT NULL
            GROUP BY query
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (since, limit),
        )
        zero_result_queries = _rows(
            conn,
            """
            SELECT query, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM usage_events
            WHERE ts >= ? AND tool = 'knowledge_search' AND success = 1
              AND COALESCE(result_count, 0) = 0 AND query IS NOT NULL
            GROUP BY query
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (since, limit),
        )
        top_artifacts = _rows(
            conn,
            """
            SELECT artifact_id, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM usage_events
            WHERE ts >= ? AND artifact_id IS NOT NULL
            GROUP BY artifact_id
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (since, limit),
        )
        errors = _rows(
            conn,
            """
            SELECT tool, error, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM usage_events
            WHERE ts >= ? AND success = 0 AND error IS NOT NULL
            GROUP BY tool, error
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (since, limit),
        )
        feedback_by_rating = _rows(
            conn,
            """
            SELECT rating, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM feedback
            WHERE ts >= ?
            GROUP BY rating
            ORDER BY count DESC, rating
            LIMIT ?
            """,
            (since, limit),
        )
        recent_negative_feedback = _rows(
            conn,
            f"""
            SELECT id, ts, rating, event_id, query, artifact_id, note
            FROM feedback
            WHERE ts >= ? AND rating IN ({','.join('?' for _ in NEGATIVE_FEEDBACK_RATINGS)})
            ORDER BY ts DESC
            LIMIT ?
            """,
            (since, *sorted(NEGATIVE_FEEDBACK_RATINGS), limit),
        )
    finally:
        conn.close()

    improvement_candidates: list[dict[str, Any]] = []
    for row in zero_result_queries[:limit]:
        improvement_candidates.append({"type": "zero_result_query", **row})
    for row in recent_negative_feedback[:limit]:
        improvement_candidates.append({"type": f"feedback_{row['rating']}", **row})
    for row in errors[:limit]:
        improvement_candidates.append({"type": "tool_error", **row})

    return {
        "success": True,
        "usage_db_path": str(usage_db),
        "since": since,
        "days": days,
        "total_events": total_events,
        "feedback_count": feedback_count,
        "avg_latency_ms": None if avg_latency is None else round(float(avg_latency), 1),
        "top_tools": top_tools,
        "top_queries": top_queries,
        "zero_result_queries": zero_result_queries,
        "top_artifacts": top_artifacts,
        "errors": errors,
        "feedback_by_rating": feedback_by_rating,
        "recent_negative_feedback": recent_negative_feedback,
        "improvement_candidates": improvement_candidates[:limit],
    }


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _handle_search(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query is required", success=False)

    limit = _coerce_int(args.get("limit"), default=8, minimum=1, maximum=30)
    artifact_type = str(args.get("artifact_type") or "").strip()
    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    root: Path | None = None
    db_path: Path | None = None

    try:
        root = _repo_root()
        db_path, meta = _ensure_index(root, rebuild=rebuild)
        index = _index_module(root)
        fetch_limit = limit * 3 if artifact_type else limit
        rows = index.search_index(db_path, query, limit=fetch_limit)
        if artifact_type:
            rows = [row for row in rows if row.get("type") == artifact_type]
        rows = rows[:limit]
        event_id = _record_usage(
            root,
            tool="knowledge_search",
            success=True,
            query=query,
            artifact_type=artifact_type,
            limit_value=limit,
            rebuild_requested=rebuild,
            rebuilt=bool(meta.get("rebuilt")),
            result_count=len(rows),
            top_ids=[str(row.get("id")) for row in rows[:5]],
            top_types=[str(row.get("type")) for row in rows[:5]],
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_result(
            {
                "success": True,
                "query": query,
                "artifact_type": artifact_type or None,
                "limit": limit,
                "results": rows,
                "usage_event_id": event_id,
                **meta,
            }
        )
    except Exception as exc:
        message = f"knowledge_search failed: {type(exc).__name__}: {exc}"
        event_id = _record_usage(
            root,
            tool="knowledge_search",
            success=False,
            query=query,
            artifact_type=artifact_type,
            limit_value=limit,
            rebuild_requested=rebuild,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=event_id)


def _handle_get(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return tool_error("artifact_id is required", success=False)

    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    include_neighbors = _coerce_bool(args.get("include_neighbors"), default=False)
    root: Path | None = None
    db_path: Path | None = None

    try:
        root = _repo_root()
        db_path, meta = _ensure_index(root, rebuild=rebuild)
        index = _index_module(root)
        artifact = index.get_artifact(db_path, artifact_id)
        if artifact is None:
            message = f"Artifact not found: {artifact_id}"
            event_id = _record_usage(
                root,
                tool="knowledge_get",
                success=False,
                artifact_id=artifact_id,
                rebuild_requested=rebuild,
                rebuilt=bool(meta.get("rebuilt")),
                error=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                context=context,
            )
            return tool_error(
                message,
                success=False,
                artifact_id=artifact_id,
                usage_event_id=event_id,
                **meta,
            )
        neighbors = index.get_neighbors(db_path, artifact_id) if include_neighbors else None
        event_id = _record_usage(
            root,
            tool="knowledge_get",
            success=True,
            artifact_id=artifact_id,
            rebuild_requested=rebuild,
            rebuilt=bool(meta.get("rebuilt")),
            result_count=1,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        payload: dict[str, Any] = {
            "success": True,
            "artifact": artifact,
            "usage_event_id": event_id,
            **meta,
        }
        if neighbors is not None:
            payload["neighbors"] = neighbors
        return tool_result(payload)
    except Exception as exc:
        message = f"knowledge_get failed: {type(exc).__name__}: {exc}"
        event_id = _record_usage(
            root,
            tool="knowledge_get",
            success=False,
            artifact_id=artifact_id,
            rebuild_requested=rebuild,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=event_id)


def _handle_neighbors(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return tool_error("artifact_id is required", success=False)

    limit = _coerce_int(args.get("limit"), default=20, minimum=1, maximum=50)
    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    root: Path | None = None
    db_path: Path | None = None

    try:
        root = _repo_root()
        db_path, meta = _ensure_index(root, rebuild=rebuild)
        index = _index_module(root)
        artifact = index.get_artifact(db_path, artifact_id)
        if artifact is None:
            message = f"Artifact not found: {artifact_id}"
            event_id = _record_usage(
                root,
                tool="knowledge_neighbors",
                success=False,
                artifact_id=artifact_id,
                limit_value=limit,
                rebuild_requested=rebuild,
                rebuilt=bool(meta.get("rebuilt")),
                error=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                context=context,
            )
            return tool_error(
                message,
                success=False,
                artifact_id=artifact_id,
                usage_event_id=event_id,
                **meta,
            )
        rows = index.get_neighbors(db_path, artifact_id)[:limit]
        event_id = _record_usage(
            root,
            tool="knowledge_neighbors",
            success=True,
            artifact_id=artifact_id,
            limit_value=limit,
            rebuild_requested=rebuild,
            rebuilt=bool(meta.get("rebuilt")),
            result_count=len(rows),
            top_ids=[str(row.get("id")) for row in rows[:5]],
            top_types=[str(row.get("type")) for row in rows[:5]],
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_result(
            {
                "success": True,
                "artifact_id": artifact_id,
                "neighbors": rows,
                "limit": limit,
                "usage_event_id": event_id,
                **meta,
            }
        )
    except Exception as exc:
        message = f"knowledge_neighbors failed: {type(exc).__name__}: {exc}"
        event_id = _record_usage(
            root,
            tool="knowledge_neighbors",
            success=False,
            artifact_id=artifact_id,
            limit_value=limit,
            rebuild_requested=rebuild,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=event_id)


def _handle_feedback(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    rating = str(args.get("rating") or "").strip().lower()
    if rating not in FEEDBACK_RATINGS:
        return tool_error(
            f"rating must be one of: {', '.join(sorted(FEEDBACK_RATINGS))}",
            success=False,
        )
    event_id_raw = args.get("event_id")
    try:
        event_id = int(event_id_raw) if event_id_raw is not None else None
    except Exception:
        return tool_error("event_id must be an integer when provided", success=False)

    query = str(args.get("query") or "")
    artifact_id = str(args.get("artifact_id") or "")
    note = str(args.get("note") or "")
    root: Path | None = None
    try:
        root = _repo_root()
        feedback_id = _record_feedback(
            root,
            rating=rating,
            event_id=event_id,
            query=query,
            artifact_id=artifact_id,
            note=note,
            context=context,
        )
        usage_event_id = _record_usage(
            root,
            tool="knowledge_feedback",
            success=True,
            query=query,
            artifact_id=artifact_id,
            result_count=1,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=_usage_db_path(root),
            context=context,
        )
        return tool_result(
            {
                "success": True,
                "feedback_id": feedback_id,
                "usage_event_id": usage_event_id,
                "rating": rating,
                "event_id": event_id,
                "usage_db_path": str(_usage_db_path(root)),
            }
        )
    except Exception as exc:
        message = f"knowledge_feedback failed: {type(exc).__name__}: {exc}"
        usage_event_id = _record_usage(
            root,
            tool="knowledge_feedback",
            success=False,
            query=query,
            artifact_id=artifact_id,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=usage_event_id)


def _handle_usage_report(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    days = _coerce_int(args.get("days"), default=14, minimum=1, maximum=365)
    limit = _coerce_int(args.get("limit"), default=10, minimum=1, maximum=50)
    root: Path | None = None
    try:
        root = _repo_root()
        report = _usage_report(root, days=days, limit=limit)
        usage_event_id = _record_usage(
            root,
            tool="knowledge_usage_report",
            success=True,
            limit_value=limit,
            result_count=int(report.get("total_events") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=_usage_db_path(root),
            context=context,
        )
        report["usage_event_id"] = usage_event_id
        return tool_result(report)
    except Exception as exc:
        message = f"knowledge_usage_report failed: {type(exc).__name__}: {exc}"
        usage_event_id = _record_usage(
            root,
            tool="knowledge_usage_report",
            success=False,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=usage_event_id)


def register(ctx) -> None:
    """Register native tools for the local knowledge index."""
    for name, schema, handler, emoji in (
        ("knowledge_search", KNOWLEDGE_SEARCH_SCHEMA, _handle_search, "🗺️"),
        ("knowledge_get", KNOWLEDGE_GET_SCHEMA, _handle_get, "📄"),
        ("knowledge_neighbors", KNOWLEDGE_NEIGHBORS_SCHEMA, _handle_neighbors, "🔗"),
        ("knowledge_feedback", KNOWLEDGE_FEEDBACK_SCHEMA, _handle_feedback, "📝"),
        ("knowledge_usage_report", KNOWLEDGE_USAGE_REPORT_SCHEMA, _handle_usage_report, "📊"),
    ):
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=check_knowledge_available,
            emoji=emoji,
        )
