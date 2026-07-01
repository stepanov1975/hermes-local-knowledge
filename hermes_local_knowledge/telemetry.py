"""Usage and feedback telemetry for local knowledge tools."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .runtime import _usage_db_path
from .schemas import FEEDBACK_RATINGS, NEGATIVE_FEEDBACK_RATINGS

RECENT_LIVE_ERROR_DAYS = 3
PROBE_QUERIES = {"demo", "sentinel unlikely", "xxxx"}


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


def _root_scope_sql() -> str:
    return """
        CASE
            WHEN root = ? THEN 'live'
            WHEN root LIKE '/tmp/pytest-%' OR root LIKE '%/pytest-%' THEN 'test_tmp'
            ELSE 'other_or_null'
        END
    """


def _rating_buckets(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buckets: dict[str, dict[str, Any]] = {}
    unknown: list[dict[str, Any]] = []
    for row in rows:
        raw_rating = str(row.get("rating") or "")
        bucket = raw_rating if raw_rating in FEEDBACK_RATINGS else "other"
        if raw_rating and raw_rating not in FEEDBACK_RATINGS:
            unknown.append(dict(row))
        target = buckets.setdefault(bucket, {"rating": bucket, "count": 0, "last_seen": None})
        target["count"] += int(row.get("count") or 0)
        if row.get("last_seen") and (target["last_seen"] is None or row["last_seen"] > target["last_seen"]):
            target["last_seen"] = row["last_seen"]
    return sorted(buckets.values(), key=lambda row: (-row["count"], row["rating"])), unknown


def _is_probe_query(query: Any) -> bool:
    return str(query or "").strip().lower() in PROBE_QUERIES


def _split_resolved_feedback(
    feedback_rows: list[dict[str, Any]], positive_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    latest_positive = {row["effective_query"]: row for row in positive_rows if row.get("effective_query")}
    unresolved: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for row in feedback_rows:
        query = row.get("effective_query") or row.get("query")
        positive = latest_positive.get(query)
        if positive and positive.get("last_positive_feedback") and positive["last_positive_feedback"] > row["ts"]:
            resolved.append({**row, "resolved_at": positive["last_positive_feedback"]})
        else:
            unresolved.append(row)
    return unresolved, resolved


def _split_resolved_zero_results(
    zero_rows: list[dict[str, Any]], success_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    latest_success = {row["query"]: row for row in success_rows}
    unresolved: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for row in zero_rows:
        success = latest_success.get(row["query"])
        if success and success.get("last_success") and success["last_success"] > row["last_seen"]:
            resolved.append(
                {
                    **row,
                    "resolved_at": success["last_success"],
                    "resolved_result_count": success.get("max_result_count"),
                }
            )
        else:
            unresolved.append(row)
    return unresolved, resolved


def _usage_report(root: Path, *, days: int, limit: int) -> dict[str, Any]:
    usage_db = _usage_db_path(root)
    now = datetime.now(timezone.utc)
    since_dt = now - timedelta(days=days)
    since = since_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    recent_live_errors_since_dt = max(since_dt, now - timedelta(days=RECENT_LIVE_ERROR_DAYS))
    recent_live_errors_since = recent_live_errors_since_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    root_text = str(root)
    if not usage_db.exists():
        return {
            "success": True,
            "usage_db_path": str(usage_db),
            "live_root": root_text,
            "since": since,
            "days": days,
            "total_events": 0,
            "live_total_events": 0,
            "feedback_count": 0,
            "live_feedback_count": 0,
            "root_breakdown": [],
            "feedback_root_breakdown": [],
            "top_tools": [],
            "top_queries": [],
            "zero_result_queries": [],
            "live_zero_result_queries": [],
            "unresolved_zero_result_queries": [],
            "active_zero_result_queries": [],
            "probe_zero_result_queries": [],
            "resolved_zero_result_queries": [],
            "top_artifacts": [],
            "errors": [],
            "live_errors": [],
            "recent_live_errors_since": recent_live_errors_since,
            "recent_live_errors": [],
            "feedback_by_rating": [],
            "feedback_rating_buckets": [],
            "unknown_feedback_ratings": [],
            "recent_negative_feedback": [],
            "live_recent_negative_feedback": [],
            "unresolved_negative_feedback": [],
            "resolved_negative_feedback": [],
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
        live_total_events = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE ts >= ? AND root = ?",
            (since, root_text),
        ).fetchone()[0]
        feedback_count = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE ts >= ?",
            (since,),
        ).fetchone()[0]
        live_feedback_count = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE ts >= ? AND root = ?",
            (since, root_text),
        ).fetchone()[0]
        avg_latency = conn.execute(
            "SELECT AVG(latency_ms) FROM usage_events WHERE ts >= ? AND latency_ms IS NOT NULL",
            (since,),
        ).fetchone()[0]
        root_breakdown = _rows(
            conn,
            f"""
            SELECT root_scope, COUNT(*) AS count, SUM(success) AS successes,
                   COUNT(*) - SUM(success) AS errors, MAX(ts) AS last_seen
            FROM (
                SELECT {_root_scope_sql()} AS root_scope, success, ts
                FROM usage_events
                WHERE ts >= ?
            )
            GROUP BY root_scope
            ORDER BY count DESC, root_scope
            """,
            (root_text, since),
        )
        feedback_root_breakdown = _rows(
            conn,
            f"""
            SELECT root_scope, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM (
                SELECT {_root_scope_sql()} AS root_scope, ts
                FROM feedback
                WHERE ts >= ?
            )
            GROUP BY root_scope
            ORDER BY count DESC, root_scope
            """,
            (root_text, since),
        )
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
        live_zero_result_queries = _rows(
            conn,
            """
            SELECT query, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM usage_events
            WHERE ts >= ? AND root = ? AND tool = 'knowledge_search' AND success = 1
              AND COALESCE(result_count, 0) = 0 AND query IS NOT NULL
            GROUP BY query
            ORDER BY count DESC, last_seen DESC
            """,
            (since, root_text),
        )
        live_successful_queries = _rows(
            conn,
            """
            SELECT query, MAX(ts) AS last_success, MAX(result_count) AS max_result_count
            FROM usage_events
            WHERE ts >= ? AND root = ? AND tool = 'knowledge_search' AND success = 1
              AND COALESCE(result_count, 0) > 0 AND query IS NOT NULL
            GROUP BY query
            """,
            (since, root_text),
        )
        unresolved_zero_result_queries, resolved_zero_result_queries = _split_resolved_zero_results(
            live_zero_result_queries,
            live_successful_queries,
        )
        active_zero_result_queries = [row for row in unresolved_zero_result_queries if not _is_probe_query(row["query"])]
        probe_zero_result_queries = [row for row in unresolved_zero_result_queries if _is_probe_query(row["query"])]
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
        live_errors = _rows(
            conn,
            """
            SELECT client, tool, error, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM usage_events
            WHERE ts >= ? AND root = ? AND success = 0 AND error IS NOT NULL
            GROUP BY client, tool, error
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (since, root_text, limit),
        )
        recent_live_errors = _rows(
            conn,
            """
            SELECT client, tool, error, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM usage_events
            WHERE ts >= ? AND root = ? AND success = 0 AND error IS NOT NULL
            GROUP BY client, tool, error
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (recent_live_errors_since, root_text, limit),
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
        feedback_rating_buckets, unknown_feedback_ratings = _rating_buckets(feedback_by_rating)
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
        live_recent_negative_feedback = _rows(
            conn,
            f"""
            SELECT f.id, f.ts, f.rating, f.event_id, f.query, f.artifact_id, f.note,
                   COALESCE(NULLIF(f.query, ''), e.query) AS effective_query
            FROM feedback f
            LEFT JOIN usage_events e ON f.event_id = e.id
            WHERE f.ts >= ? AND f.root = ? AND f.rating IN ({','.join('?' for _ in NEGATIVE_FEEDBACK_RATINGS)})
            ORDER BY f.ts DESC
            LIMIT ?
            """,
            (since, root_text, *sorted(NEGATIVE_FEEDBACK_RATINGS), limit),
        )
        live_positive_feedback_queries = _rows(
            conn,
            """
            SELECT COALESCE(NULLIF(f.query, ''), e.query) AS effective_query,
                   MAX(f.ts) AS last_positive_feedback
            FROM feedback f
            LEFT JOIN usage_events e ON f.event_id = e.id
            WHERE f.ts >= ? AND f.root = ? AND f.rating = 'useful'
            GROUP BY effective_query
            """,
            (since, root_text),
        )
        unresolved_negative_feedback, resolved_negative_feedback = _split_resolved_feedback(
            live_recent_negative_feedback,
            live_positive_feedback_queries,
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
    for row in active_zero_result_queries[:limit]:
        improvement_candidates.append({"type": "zero_result_query", **row})
    for row in unresolved_negative_feedback[:limit]:
        improvement_candidates.append({"type": f"feedback_{row['rating']}", **row})
    for row in recent_live_errors[:limit]:
        improvement_candidates.append({"type": "tool_error", **row})

    return {
        "success": True,
        "usage_db_path": str(usage_db),
        "live_root": root_text,
        "since": since,
        "days": days,
        "total_events": total_events,
        "live_total_events": live_total_events,
        "feedback_count": feedback_count,
        "live_feedback_count": live_feedback_count,
        "avg_latency_ms": None if avg_latency is None else round(float(avg_latency), 1),
        "root_breakdown": root_breakdown,
        "feedback_root_breakdown": feedback_root_breakdown,
        "top_tools": top_tools,
        "top_queries": top_queries,
        "zero_result_queries": zero_result_queries,
        "live_zero_result_queries": live_zero_result_queries[:limit],
        "unresolved_zero_result_queries": unresolved_zero_result_queries[:limit],
        "active_zero_result_queries": active_zero_result_queries[:limit],
        "probe_zero_result_queries": probe_zero_result_queries[:limit],
        "resolved_zero_result_queries": resolved_zero_result_queries[:limit],
        "top_artifacts": top_artifacts,
        "errors": errors,
        "live_errors": live_errors,
        "recent_live_errors_since": recent_live_errors_since,
        "recent_live_errors": recent_live_errors,
        "feedback_by_rating": feedback_by_rating,
        "feedback_rating_buckets": feedback_rating_buckets,
        "unknown_feedback_ratings": unknown_feedback_ratings,
        "recent_negative_feedback": recent_negative_feedback,
        "live_recent_negative_feedback": live_recent_negative_feedback,
        "unresolved_negative_feedback": unresolved_negative_feedback,
        "resolved_negative_feedback": resolved_negative_feedback,
        "latest_index_metadata": latest_index_rows[0] if latest_index_rows else None,
        "recent_builds": recent_builds,
        "improvement_candidates": improvement_candidates[:limit],
    }
