"""Usage and feedback telemetry for local knowledge tools."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .runtime import _usage_db_path
from .schemas import NEGATIVE_FEEDBACK_RATINGS


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


def _json_object(value: Any) -> str:
    return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False, sort_keys=True)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None

USAGE_EVENT_COLUMNS: dict[str, str] = {
    "client": "TEXT NOT NULL DEFAULT 'native'",
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
    "plugin_version": "TEXT",
    "source_root_source": "TEXT",
    "state_dir_source": "TEXT",
    "include_markdown_docs_source": "TEXT",
    "index_exists": "INTEGER",
    "index_mtime": "TEXT",
    "index_age_seconds": "INTEGER",
    "index_artifact_count": "INTEGER",
    "index_edge_count": "INTEGER",
    "index_artifact_counts_json": "TEXT NOT NULL DEFAULT '{}'",
    "index_metadata_error": "TEXT",
    "build_duration_ms": "INTEGER",
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
            client TEXT NOT NULL DEFAULT 'native',
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
            plugin_version TEXT,
            source_root_source TEXT,
            state_dir_source TEXT,
            include_markdown_docs_source TEXT,
            index_exists INTEGER,
            index_mtime TEXT,
            index_age_seconds INTEGER,
            index_artifact_count INTEGER,
            index_edge_count INTEGER,
            index_artifact_counts_json TEXT NOT NULL DEFAULT '{}',
            index_metadata_error TEXT,
            build_duration_ms INTEGER,
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

def _usage_connect(root: Path | None, usage_db_path: Path | None = None) -> sqlite3.Connection:
    if usage_db_path is None:
        if root is None:
            raise ValueError("root or usage_db_path is required")
        resolved_usage_db = _usage_db_path(root)
    else:
        resolved_usage_db = usage_db_path
    resolved_usage_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved_usage_db), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
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
    client: str = "native",
    index_metadata: dict[str, Any] | None = None,
    usage_db_path: Path | None = None,
) -> int | None:
    if root is None and usage_db_path is None:
        return None
    try:
        context = context or {}
        index_metadata = index_metadata or {}
        artifact_counts = index_metadata.get("artifact_counts_by_type")
        conn = _usage_connect(root, usage_db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO usage_events (
                    ts, tool, client, session_id, task_id, tool_call_id, query,
                    artifact_id, artifact_type, limit_value, rebuild_requested,
                    rebuilt, success, error, result_count, top_ids_json,
                    top_types_json, latency_ms, plugin_version, source_root_source,
                    state_dir_source, include_markdown_docs_source, index_exists,
                    index_mtime, index_age_seconds, index_artifact_count,
                    index_edge_count, index_artifact_counts_json,
                    index_metadata_error, build_duration_ms, root, db_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    tool,
                    _clean_text(client, limit=40) or "native",
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
                    _clean_text(index_metadata.get("plugin_version") or __version__, limit=80) or None,
                    _clean_text(index_metadata.get("source_root_source"), limit=80) or None,
                    _clean_text(index_metadata.get("state_dir_source"), limit=80) or None,
                    _clean_text(index_metadata.get("include_markdown_docs_source"), limit=80) or None,
                    None if "index_exists" not in index_metadata else (1 if index_metadata.get("index_exists") else 0),
                    _clean_text(index_metadata.get("index_mtime"), limit=80) or None,
                    _int_or_none(index_metadata.get("index_age_seconds")),
                    _int_or_none(index_metadata.get("artifact_count")),
                    _int_or_none(index_metadata.get("edge_count")),
                    _json_object(artifact_counts),
                    _clean_text(index_metadata.get("index_metadata_error"), limit=1000) or None,
                    _int_or_none(index_metadata.get("build_duration_ms")),
                    str(root)
                    if root is not None
                    else (_clean_text(index_metadata.get("root") or index_metadata.get("source_root"), limit=1000) or None),
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


def _decode_json_object(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(text or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_index_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        row["index_artifact_counts"] = _decode_json_object(row.pop("index_artifact_counts_json", None))
    return rows


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
            "latest_index_metadata": None,
            "recent_builds": [],
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
            SELECT client, tool, COUNT(*) AS count, SUM(success) AS successes,
                   COUNT(*) - SUM(success) AS errors,
                   ROUND(AVG(latency_ms), 1) AS avg_latency_ms
            FROM usage_events
            WHERE ts >= ?
            GROUP BY client, tool
            ORDER BY count DESC, client, tool
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
            SELECT client, tool, error, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM usage_events
            WHERE ts >= ? AND success = 0 AND error IS NOT NULL
            GROUP BY client, tool, error
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
        latest_index_rows = _normalize_index_rows(
            _rows(
                conn,
                """
                SELECT id, ts, client, tool, plugin_version, root, db_path,
                       source_root_source, state_dir_source,
                       include_markdown_docs_source, index_exists, index_mtime,
                       index_age_seconds, index_artifact_count,
                       index_edge_count, index_artifact_counts_json,
                       index_metadata_error, build_duration_ms, rebuilt
                FROM usage_events
                WHERE ts >= ? AND (index_exists IS NOT NULL OR index_mtime IS NOT NULL OR index_artifact_count IS NOT NULL)
                ORDER BY ts DESC, id DESC
                LIMIT 1
                """,
                (since,),
            )
        )
        recent_builds = _normalize_index_rows(
            _rows(
                conn,
                """
                SELECT id, ts, client, tool, plugin_version, root, db_path,
                       source_root_source, state_dir_source, index_mtime,
                       index_artifact_count, index_edge_count,
                       index_artifact_counts_json, build_duration_ms, rebuilt
                FROM usage_events
                WHERE ts >= ? AND success = 1 AND rebuilt = 1
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (since, limit),
            )
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
        "latest_index_metadata": latest_index_rows[0] if latest_index_rows else None,
        "recent_builds": recent_builds,
        "improvement_candidates": improvement_candidates[:limit],
    }
