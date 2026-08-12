"""Usage and feedback telemetry for local knowledge tools."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import resolve_config

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
LOOKUP_TOOLS = {"knowledge_search", "knowledge_get", "knowledge_neighbors"}
GENERAL_TELEMETRY_TIMEOUT_SECONDS = 1.0

RECENT_LIVE_ERROR_DAYS = 3
PROBE_QUERIES = {"demo", "sentinel unlikely", "xxxx"}


class FeedbackDatabaseLockedError(RuntimeError):
    """Raised when strict feedback cannot acquire its one bounded write lock."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _clean_text(value: Any, *, limit: int = 1000) -> str:
    text = str(value or "")
    text = " ".join(text.replace("\x00", "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _exact_text(value: Any) -> str:
    """Preserve local replay values without collapsing whitespace or truncating."""

    return str(value or "")

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
    "baseline_top_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    "route_feedback_id": "INTEGER",
    "route_artifact_id": "TEXT",
    "route_outcome": "TEXT NOT NULL DEFAULT 'none'",
    "index_jsonl_sha256": "TEXT",
    "index_format_version": "INTEGER",
    "feedback_max_id": "INTEGER DEFAULT -1",
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
    "expected_artifact_id": "TEXT",
    "resolves_feedback_id": "INTEGER",
    "linkage_status": "TEXT NOT NULL DEFAULT 'legacy'",
}


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError as exc:
                # Concurrent first-use connections can both observe the old
                # schema. SQLite serializes the ALTERs, so the loser sees a
                # duplicate after the winner commits. Recheck rather than
                # dropping an otherwise valid telemetry write.
                if "duplicate column name" not in str(exc).casefold():
                    raise
                current = {
                    str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
                }
                if name not in current:
                    raise
                existing.update(current)
            existing.add(name)

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
            baseline_top_ids_json TEXT NOT NULL DEFAULT '[]',
            route_feedback_id INTEGER,
            route_artifact_id TEXT,
            route_outcome TEXT NOT NULL DEFAULT 'none',
            index_jsonl_sha256 TEXT,
            index_format_version INTEGER,
            feedback_max_id INTEGER DEFAULT -1,
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
            root TEXT,
            expected_artifact_id TEXT,
            resolves_feedback_id INTEGER,
            linkage_status TEXT NOT NULL DEFAULT 'legacy'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS implicit_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            search_event_id INTEGER NOT NULL,
            query TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            root TEXT NOT NULL,
            UNIQUE(search_event_id, artifact_id)
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_root_id ON feedback(root, id DESC)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_resolution_unique "
        "ON feedback(resolves_feedback_id) WHERE resolves_feedback_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_route_lookup "
        "ON feedback(root, linkage_status, rating, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_implicit_feedback_route_lookup "
        "ON implicit_feedback(root, id DESC)"
    )


def _usage_connect(
    root: Path | None,
    usage_db_path: Path | None = None,
    *,
    initialize: bool = True,
) -> sqlite3.Connection:
    if usage_db_path is None:
        if root is None:
            raise ValueError("root or usage_db_path is required")
        resolved_usage_db = resolve_config().state_dir / "usage.sqlite"
    else:
        resolved_usage_db = usage_db_path
    resolved_usage_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved_usage_db), timeout=GENERAL_TELEMETRY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(GENERAL_TELEMETRY_TIMEOUT_SECONDS * 1000)}")
    if initialize:
        _init_usage_db(conn)
        conn.commit()
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
    baseline_top_ids: list[str] | None = None,
    route_feedback_id: int | None = None,
    route_artifact_id: str | None = None,
    route_outcome: str = "none",
    feedback_max_id: int | None = -1,
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
        conn = _usage_connect(root, usage_db_path, initialize=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _init_usage_db(conn)
            cur = conn.execute(
                """
                INSERT INTO usage_events (
                    ts, tool, client, session_id, task_id, tool_call_id, query,
                    artifact_id, artifact_type, limit_value, rebuild_requested,
                    rebuilt, success, error, result_count, top_ids_json,
                    top_types_json, baseline_top_ids_json, route_feedback_id,
                    route_artifact_id, route_outcome, feedback_max_id, latency_ms,
                    plugin_version, source_root_source, state_dir_source,
                    include_markdown_docs_source, index_exists, index_mtime,
                    index_age_seconds, index_artifact_count, index_edge_count,
                    index_artifact_counts_json, index_metadata_error,
                    build_duration_ms, root, db_path, index_jsonl_sha256,
                    index_format_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    tool,
                    _clean_text(client, limit=40) or "native",
                    context.get("session_id") or None,
                    context.get("task_id") or None,
                    context.get("tool_call_id") or None,
                    _exact_text(query) or None,
                    _exact_text(artifact_id) or None,
                    _clean_text(artifact_type, limit=80) or None,
                    limit_value,
                    1 if rebuild_requested else 0,
                    None if rebuilt is None else (1 if rebuilt else 0),
                    1 if success else 0,
                    _clean_text(error, limit=1000) or None,
                    result_count,
                    _json_list(top_ids),
                    _json_list(top_types),
                    _json_list(baseline_top_ids),
                    route_feedback_id,
                    _exact_text(route_artifact_id) or None,
                    _clean_text(route_outcome, limit=40) or "none",
                    feedback_max_id,
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
                    _clean_text(index_metadata.get("jsonl_sha256"), limit=80) or None,
                    _int_or_none(index_metadata.get("index_format_version")),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()
    except Exception:
        # Telemetry must never break the lookup tools.
        return None


def record_implicit_feedback(
    root: Path,
    *,
    search_event_id: int,
    query: str,
    artifact_id: str,
    session_id: str,
    task_id: str,
    usage_db_path: Path,
) -> bool:
    """Persist one idempotent consumed-result signal."""

    conn = _usage_connect(root, usage_db_path, initialize=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_usage_db(conn)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO implicit_feedback (
                ts, search_event_id, query, artifact_id, session_id, task_id, root
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                search_event_id,
                _exact_text(query),
                _exact_text(artifact_id),
                _clean_text(session_id, limit=128),
                _clean_text(task_id, limit=128),
                str(root),
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def _record_feedback(
    root: Path,
    *,
    rating: str,
    event_id: int | None,
    query: str,
    artifact_id: str,
    note: str,
    context: dict[str, str],
    expected_artifact_id: str = "",
    resolves_feedback_id: int | None = None,
    artifact_exists: Callable[[str], bool] | None = None,
    usage_started_at: float | None = None,
    usage_db_path: Path | None = None,
) -> tuple[int, int]:
    root_text = str(root)
    canonical_query = _exact_text(query).strip()
    artifact_id = _exact_text(artifact_id).strip()
    expected_artifact_id = _exact_text(expected_artifact_id).strip()
    note = _exact_text(note)
    conn: sqlite3.Connection | None = None
    try:
        conn = _usage_connect(root, usage_db_path, initialize=False)
        conn.execute("BEGIN IMMEDIATE")
        _init_usage_db(conn)

        event: sqlite3.Row | None = None
        if event_id is not None:
            event = conn.execute("SELECT * FROM usage_events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise ValueError(f"usage event does not exist: {event_id}")
            if str(event["root"] or "") != root_text:
                raise ValueError("usage event does not belong to the current root")
            event_tool = str(event["tool"] or "")
            if event_tool not in LOOKUP_TOOLS:
                raise ValueError("usage event tool is not a supported lookup tool")
            if int(event["success"] or 0) != 1:
                raise ValueError("usage event is not a successful lookup")
            # Search handlers remove outer whitespace before execution. Mirror
            # that canonical form for legacy/directly-recorded events while
            # preserving every internal whitespace character.
            event_query = str(event["query"] or "").strip()
            if canonical_query and canonical_query != event_query:
                raise ValueError("query does not match the referenced usage event")
            if event_tool == "knowledge_search":
                canonical_query = event_query
                try:
                    returned_ids = json.loads(str(event["top_ids_json"] or "[]"))
                except json.JSONDecodeError as exc:
                    raise ValueError("referenced search result IDs are invalid") from exc
                if not isinstance(returned_ids, list):
                    raise ValueError("referenced search result IDs are invalid")
                if artifact_id and artifact_id not in {str(value) for value in returned_ids}:
                    raise ValueError("artifact_id is absent from the recorded result page")
            elif event_tool == "knowledge_get":
                if artifact_id and artifact_id != str(event["artifact_id"] or ""):
                    raise ValueError("artifact_id does not match the referenced artifact lookup")
                if expected_artifact_id or resolves_feedback_id is not None:
                    raise ValueError(
                        "expected_artifact_id and resolves_feedback_id require search feedback"
                    )
            else:
                try:
                    neighbor_ids = json.loads(str(event["top_ids_json"] or "[]"))
                except json.JSONDecodeError as exc:
                    raise ValueError("referenced neighbor result IDs are invalid") from exc
                if not isinstance(neighbor_ids, list):
                    raise ValueError("referenced neighbor result IDs are invalid")
                valid_neighbor_ids = {str(value) for value in neighbor_ids}
                valid_neighbor_ids.add(str(event["artifact_id"] or ""))
                if artifact_id and artifact_id not in valid_neighbor_ids:
                    raise ValueError("artifact_id is absent from the referenced neighbor lookup")
                if expected_artifact_id or resolves_feedback_id is not None:
                    raise ValueError(
                        "expected_artifact_id and resolves_feedback_id require search feedback"
                    )
            linkage_status = "verified_event"
        elif canonical_query:
            linkage_status = "direct_query"
        elif artifact_id:
            linkage_status = "artifact_only"
        else:
            linkage_status = "unscoped"

        if linkage_status in {"artifact_only", "unscoped"} and (
            expected_artifact_id or resolves_feedback_id is not None
        ):
            raise ValueError(
                "expected_artifact_id and resolves_feedback_id require replayable search feedback"
            )
        if expected_artifact_id:
            if rating not in NEGATIVE_FEEDBACK_RATINGS:
                raise ValueError("expected_artifact_id requires a negative search-quality rating")
            if artifact_exists is None or not artifact_exists(expected_artifact_id):
                raise ValueError(
                    "expected artifact does not exist in the current managed index: "
                    f"{expected_artifact_id}"
                )

        if resolves_feedback_id is not None:
            if rating != "useful":
                raise ValueError("resolves_feedback_id requires rating='useful'")
            if not artifact_id:
                raise ValueError("resolves_feedback_id requires artifact_id")
            if event is None or str(event["tool"] or "") != "knowledge_search":
                raise ValueError("resolves_feedback_id requires a successful search event")
            if artifact_exists is None or not artifact_exists(artifact_id):
                raise ValueError(f"accepted artifact does not exist in the current index: {artifact_id}")
            negative = conn.execute(
                """
                SELECT f.id, f.rating, f.root, f.query, f.artifact_id,
                       f.expected_artifact_id, f.linkage_status, f.event_id,
                       e.tool AS event_tool, e.success AS event_success,
                       e.root AS event_root, e.query AS event_query,
                       e.top_ids_json AS event_top_ids_json
                FROM feedback AS f
                LEFT JOIN usage_events AS e ON e.id = f.event_id
                WHERE f.id = ?
                """,
                (resolves_feedback_id,),
            ).fetchone()
            if negative is None:
                raise ValueError(f"feedback row does not exist: {resolves_feedback_id}")
            if str(negative["root"] or "") != root_text:
                raise ValueError("resolved feedback belongs to a different configured root")
            parent_linkage = str(negative["linkage_status"] or "")
            parent_query = _exact_text(negative["query"]).strip()
            if parent_linkage == "verified_event":
                parent_is_replayable = bool(parent_query) and _verified_search_link(
                    feedback_query=negative["query"],
                    feedback_artifact_id=negative["artifact_id"],
                    event_tool=negative["event_tool"],
                    event_success=negative["event_success"],
                    event_root=negative["event_root"],
                    event_query=negative["event_query"],
                    event_top_ids_json=negative["event_top_ids_json"],
                    root=root_text,
                )
            else:
                parent_is_replayable = (
                    parent_linkage in {"direct_query", "legacy"} and bool(parent_query)
                )
            if not parent_is_replayable:
                raise ValueError("resolved feedback row has no replayable search intent")
            if str(negative["rating"] or "") not in NEGATIVE_FEEDBACK_RATINGS:
                raise ValueError("resolved feedback row is not negative")
            expected = str(negative["expected_artifact_id"] or "")
            if expected and expected != artifact_id:
                raise ValueError("accepted artifact does not match the expected artifact target")
            already_resolved = conn.execute(
                "SELECT id FROM feedback WHERE resolves_feedback_id = ? LIMIT 1",
                (resolves_feedback_id,),
            ).fetchone()
            if already_resolved is not None:
                raise ValueError("feedback row is already explicitly resolved")

        timestamp = _utc_now()
        feedback_cur = conn.execute(
            """
            INSERT INTO feedback (
                ts, event_id, rating, query, artifact_id, note,
                session_id, task_id, tool_call_id, root, expected_artifact_id,
                resolves_feedback_id, linkage_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                event_id,
                rating,
                canonical_query or None,
                artifact_id or None,
                note or None,
                context.get("session_id") or None,
                context.get("task_id") or None,
                context.get("tool_call_id") or None,
                root_text,
                expected_artifact_id or None,
                resolves_feedback_id,
                linkage_status,
            ),
        )
        latency_ms = (
            int((time.perf_counter() - usage_started_at) * 1000)
            if usage_started_at is not None
            else None
        )
        usage_cur = conn.execute(
            """
            INSERT INTO usage_events (
                ts, tool, client, session_id, task_id, tool_call_id, query,
                artifact_id, success, result_count, latency_ms, plugin_version,
                root, db_path
            ) VALUES (?, 'knowledge_feedback', 'native', ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)
            """,
            (
                timestamp,
                context.get("session_id") or None,
                context.get("task_id") or None,
                context.get("tool_call_id") or None,
                canonical_query or None,
                artifact_id or None,
                latency_ms,
                __version__,
                root_text,
                str(usage_db_path) if usage_db_path is not None else None,
            ),
        )
        conn.commit()
        return int(feedback_cur.lastrowid or 0), int(usage_cur.lastrowid or 0)
    except sqlite3.IntegrityError as exc:
        if conn is not None:
            conn.rollback()
        if resolves_feedback_id is not None:
            raise ValueError("feedback row is already explicitly resolved") from exc
        raise
    except sqlite3.OperationalError as exc:
        if conn is not None:
            conn.rollback()
        message = str(exc).casefold()
        if "locked" in message or "busy" in message:
            raise FeedbackDatabaseLockedError(
                "feedback database is temporarily locked; try again"
            ) from exc
        raise
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()

def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _decode_json_object(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(text or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _decode_json_string_list(text: Any) -> list[str] | None:
    try:
        value = json.loads(str(text or "[]"))
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _event_succeeded(value: Any) -> bool:
    try:
        return int(value or 0) == 1
    except (TypeError, ValueError):
        return False


def _verified_search_link(
    *,
    feedback_query: Any,
    feedback_artifact_id: Any,
    event_tool: Any,
    event_success: Any,
    event_root: Any,
    event_query: Any,
    event_top_ids_json: Any,
    root: str,
) -> bool:
    if (
        str(event_tool or "") != "knowledge_search"
        or not _event_succeeded(event_success)
        or str(event_root or "") != root
        or _exact_text(feedback_query) != _exact_text(event_query)
    ):
        return False
    returned_ids = _decode_json_string_list(event_top_ids_json)
    if returned_ids is None:
        return False
    artifact_id = str(feedback_artifact_id or "")
    return not artifact_id or artifact_id in returned_ids


def _normalize_index_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        row["index_artifact_counts"] = _decode_json_object(row.pop("index_artifact_counts_json", None))
    return rows


def _root_scope_sql() -> str:
    return """
        CASE
            WHEN root = ? THEN 'live'
            WHEN REPLACE(root, CHAR(92), '/') LIKE '/tmp/pytest-%'
              OR REPLACE(root, CHAR(92), '/') LIKE '%/pytest-%'
              THEN 'test_tmp'
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


def _feedback_linkage_quality(row: dict[str, Any]) -> str:
    """Classify one feedback/event relationship without trusting stored labels."""

    if row.get("event_id") is not None:
        if row.get("event_exists") is None:
            return "orphaned_event"
        if str(row.get("feedback_root") or "") != str(row.get("event_root") or ""):
            return "root_mismatch"
    status = str(row.get("linkage_status") or "legacy")
    if status == "verified_event":
        if _verified_search_link(
            feedback_query=row.get("feedback_query"),
            feedback_artifact_id=row.get("artifact_id"),
            event_tool=row.get("event_tool"),
            event_success=row.get("event_success"),
            event_root=row.get("event_root"),
            event_query=row.get("event_query"),
            event_top_ids_json=row.get("event_top_ids_json"),
            root=str(row.get("feedback_root") or ""),
        ):
            return "verified_event"
        if str(row.get("feedback_query") or "").strip():
            return "legacy"
        if str(row.get("artifact_id") or "").strip():
            return "artifact_only"
        return "unscoped"
    if status == "direct_query":
        return "direct_query" if str(row.get("feedback_query") or "").strip() else "unscoped"
    if status in {"artifact_only", "unscoped", "legacy"}:
        return status
    return "legacy"


def _has_replayable_search_intent(row: dict[str, Any]) -> bool:
    quality = str(row.get("linkage_quality") or "")
    query = str(row.get("effective_query") or "").strip()
    if not query or quality in {"orphaned_event", "root_mismatch"}:
        return False
    if quality == "verified_event":
        return (
            str(row.get("event_tool") or "") == "knowledge_search"
            and _event_succeeded(row.get("event_success"))
        )
    if quality == "direct_query":
        return row.get("event_id") is None
    if quality == "legacy":
        return row.get("event_id") is None or (
            str(row.get("event_tool") or "") == "knowledge_search"
            and _event_succeeded(row.get("event_success"))
        )
    return False


def _has_valid_explicit_resolution(row: dict[str, Any]) -> bool:
    accepted_id = str(row.get("accepted_artifact_id") or "").strip()
    expected_id = str(row.get("expected_artifact_id") or "").strip()
    if (
        row.get("resolution_feedback_id") is None
        or str(row.get("resolution_rating") or "") != "useful"
        or not accepted_id
        or str(row.get("resolution_root") or "") != str(row.get("feedback_root") or "")
        or str(row.get("resolution_linkage_status") or "") != "verified_event"
        or row.get("resolution_event_exists") is None
        or (expected_id and expected_id != accepted_id)
    ):
        return False
    return _verified_search_link(
        feedback_query=row.get("resolution_query"),
        feedback_artifact_id=accepted_id,
        event_tool=row.get("resolution_event_tool"),
        event_success=row.get("resolution_event_success"),
        event_root=row.get("resolution_event_root"),
        event_query=row.get("resolution_event_query"),
        event_top_ids_json=row.get("resolution_event_top_ids_json"),
        root=str(row.get("feedback_root") or ""),
    )


def _split_resolved_feedback(
    feedback_rows: list[dict[str, Any]], positive_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Prefer explicit resolutions; retain the old query heuristic for legacy rows only."""

    latest_positive = {
        row["effective_query"]: row
        for row in positive_rows
        if row.get("effective_query")
    }
    unresolved: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    explicit_resolutions: list[dict[str, Any]] = []
    for source_row in feedback_rows:
        row = dict(source_row)
        row["linkage_quality"] = _feedback_linkage_quality(row)
        row["replayable_search_intent"] = _has_replayable_search_intent(row)
        if _has_valid_explicit_resolution(row):
            resolved_row = {
                **row,
                "resolution_kind": "explicit",
                "resolved_at": row.get("resolution_ts"),
            }
            resolved.append(resolved_row)
            explicit_resolutions.append(
                {
                    "feedback_id": row["id"],
                    "resolution_feedback_id": row["resolution_feedback_id"],
                    "resolved_at": row.get("resolution_ts"),
                    "accepted_artifact_id": row.get("accepted_artifact_id"),
                    "accepted_query": row.get("accepted_query"),
                }
            )
            continue

        query = row.get("effective_query") or row.get("query")
        positive = latest_positive.get(query)
        if (
            row["linkage_quality"] == "legacy"
            and positive
            and positive.get("last_positive_feedback")
            and positive["last_positive_feedback"] > row["ts"]
        ):
            resolved.append(
                {
                    **row,
                    "resolution_kind": "legacy_query_heuristic",
                    "resolved_at": positive["last_positive_feedback"],
                }
            )
        else:
            unresolved.append(row)
    return unresolved, resolved, explicit_resolutions


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


def _usage_report(
    root: Path,
    *,
    days: int,
    limit: int,
    usage_db_path: Path | None = None,
) -> dict[str, Any]:
    usage_db = usage_db_path if usage_db_path is not None else resolve_config().state_dir / "usage.sqlite"
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
            "implicit_feedback_count": 0,
            "live_implicit_feedback_count": 0,
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
            "feedback_linkage_quality": [],
            "feedback_linkage_counts": {},
            "feedback_resolution_quality": [],
            "feedback_resolution_counts": {},
            "unresolved_negative_feedback": [],
            "unresolved_verified_or_direct_negative_feedback": [],
            "unresolved_negative_with_expected_target": [],
            "unresolved_negative_without_expected_target": [],
            "resolved_negative_feedback": [],
            "explicit_resolutions": [],
            "explicit_resolution_count": 0,
            "route_outcomes": [],
            "route_verification_failures": [],
            "replay_ready_label_counts": {
                "explicit_resolution": 0,
                "verified_event": 0,
                "direct_or_legacy": 0,
                "total": 0,
            },
            "search_issue_candidates": [],
            "unresolved_negative_with_current_expected_target": [],
            "unresolved_negative_without_current_expected_target": [],
            "behaviorally_resolved_negative_feedback": [],
            "correction_candidates": [],
            "latest_index_metadata": None,
            "recent_builds": [],
            "improvement_candidates": [],
        }

    conn = _usage_connect(root, usage_db)
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
        implicit_feedback_count = conn.execute(
            "SELECT COUNT(*) FROM implicit_feedback WHERE ts >= ?",
            (since,),
        ).fetchone()[0]
        live_implicit_feedback_count = conn.execute(
            "SELECT COUNT(*) FROM implicit_feedback WHERE ts >= ? AND root = ?",
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
        feedback_linkage_rows = _rows(
            conn,
            """
            SELECT f.ts, f.event_id, f.query AS feedback_query, f.artifact_id,
                   f.root AS feedback_root, f.linkage_status,
                   e.id AS event_exists, e.root AS event_root, e.tool AS event_tool,
                   e.success AS event_success, e.query AS event_query,
                   e.top_ids_json AS event_top_ids_json
            FROM feedback AS f
            LEFT JOIN usage_events AS e ON e.id = f.event_id
            WHERE f.ts >= ?
            """,
            (since,),
        )
        linkage_buckets: dict[str, dict[str, Any]] = {}
        for row in feedback_linkage_rows:
            quality = _feedback_linkage_quality(row)
            bucket = linkage_buckets.setdefault(
                quality,
                {"linkage_quality": quality, "count": 0, "last_seen": None},
            )
            bucket["count"] += 1
            if row.get("ts") and (
                bucket["last_seen"] is None or row["ts"] > bucket["last_seen"]
            ):
                bucket["last_seen"] = row["ts"]
        feedback_linkage_quality = sorted(
            linkage_buckets.values(),
            key=lambda row: (-int(row["count"]), str(row["linkage_quality"])),
        )
        feedback_linkage_counts = {
            str(row["linkage_quality"]): int(row["count"])
            for row in feedback_linkage_quality
        }
        feedback_resolution_quality = _rows(
            conn,
            f"""
            SELECT resolution_quality, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM (
                SELECT f.ts,
                       CASE
                           WHEN f.resolves_feedback_id IS NOT NULL
                                AND f.rating = 'useful'
                                AND parent.id IS NOT NULL AND parent.root = f.root
                                AND parent.rating IN ({','.join('?' for _ in NEGATIVE_FEEDBACK_RATINGS)})
                                AND COALESCE(f.artifact_id, '') <> ''
                                AND f.linkage_status = 'verified_event'
                                AND e.id IS NOT NULL AND e.root = f.root
                                AND e.tool = 'knowledge_search' AND e.success = 1
                                AND COALESCE(f.query, '') = COALESCE(e.query, '')
                                AND json_valid(COALESCE(e.top_ids_json, '')) = 1
                                AND EXISTS (
                                    SELECT 1 FROM json_each(e.top_ids_json) AS result
                                    WHERE CAST(result.value AS TEXT) = f.artifact_id
                                )
                                AND (
                                    COALESCE(parent.expected_artifact_id, '') = ''
                                    OR parent.expected_artifact_id = f.artifact_id
                                )
                               THEN 'explicit_resolution'
                           WHEN f.resolves_feedback_id IS NOT NULL
                               THEN 'invalid_resolution'
                           WHEN f.rating IN ({','.join('?' for _ in NEGATIVE_FEEDBACK_RATINGS)})
                                AND EXISTS (
                                    SELECT 1
                                    FROM feedback AS r
                                    JOIN usage_events AS re ON re.id = r.event_id
                                    WHERE r.resolves_feedback_id = f.id
                                      AND r.rating = 'useful' AND r.root = f.root
                                      AND COALESCE(r.artifact_id, '') <> ''
                                      AND r.linkage_status = 'verified_event'
                                      AND re.root = r.root
                                      AND re.tool = 'knowledge_search' AND re.success = 1
                                      AND COALESCE(r.query, '') = COALESCE(re.query, '')
                                      AND json_valid(COALESCE(re.top_ids_json, '')) = 1
                                      AND EXISTS (
                                          SELECT 1 FROM json_each(re.top_ids_json) AS result
                                          WHERE CAST(result.value AS TEXT) = r.artifact_id
                                      )
                                      AND (
                                          COALESCE(f.expected_artifact_id, '') = ''
                                          OR f.expected_artifact_id = r.artifact_id
                                      )
                                )
                               THEN 'explicitly_resolved_negative'
                           WHEN f.rating IN ({','.join('?' for _ in NEGATIVE_FEEDBACK_RATINGS)})
                               THEN 'negative_without_explicit_resolution'
                           WHEN f.rating = 'useful' THEN 'standalone_positive'
                           ELSE 'other'
                       END AS resolution_quality
                FROM feedback AS f
                LEFT JOIN feedback AS parent ON parent.id = f.resolves_feedback_id
                LEFT JOIN usage_events AS e ON e.id = f.event_id
                WHERE f.ts >= ?
            )
            GROUP BY resolution_quality
            ORDER BY count DESC, resolution_quality
            """,
            (
                *sorted(NEGATIVE_FEEDBACK_RATINGS),
                *sorted(NEGATIVE_FEEDBACK_RATINGS),
                *sorted(NEGATIVE_FEEDBACK_RATINGS),
                since,
            ),
        )
        feedback_resolution_counts = {
            str(row["resolution_quality"]): int(row["count"])
            for row in feedback_resolution_quality
        }
        recent_negative_feedback = _rows(
            conn,
            f"""
            SELECT id, ts, rating, event_id, query, artifact_id, note,
                   expected_artifact_id, linkage_status
            FROM feedback
            WHERE ts >= ? AND rating IN ({','.join('?' for _ in NEGATIVE_FEEDBACK_RATINGS)})
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (since, *sorted(NEGATIVE_FEEDBACK_RATINGS), limit),
        )
        live_recent_negative_feedback = _rows(
            conn,
            f"""
            SELECT f.id, f.ts, f.rating, f.event_id, f.query,
                   f.query AS feedback_query, f.artifact_id,
                   f.note, f.expected_artifact_id, f.linkage_status,
                   f.root AS feedback_root,
                   COALESCE(NULLIF(f.query, ''), e.query) AS effective_query,
                   e.id AS event_exists, e.root AS event_root, e.tool AS event_tool,
                   e.success AS event_success, e.query AS event_query,
                   e.top_ids_json AS event_top_ids_json,
                   e.artifact_type AS artifact_type,
                   r.id AS resolution_feedback_id, r.ts AS resolution_ts,
                   r.rating AS resolution_rating, r.artifact_id AS accepted_artifact_id,
                   COALESCE(NULLIF(r.query, ''), re.query) AS accepted_query,
                   r.query AS resolution_query, r.root AS resolution_root,
                   r.linkage_status AS resolution_linkage_status,
                   re.id AS resolution_event_exists,
                   re.root AS resolution_event_root,
                   re.tool AS resolution_event_tool,
                   re.success AS resolution_event_success,
                   re.query AS resolution_event_query,
                   re.top_ids_json AS resolution_event_top_ids_json
            FROM feedback AS f
            LEFT JOIN usage_events AS e ON f.event_id = e.id
            LEFT JOIN feedback AS r ON r.resolves_feedback_id = f.id
            LEFT JOIN usage_events AS re ON r.event_id = re.id
            WHERE f.ts >= ? AND f.root = ?
              AND f.rating IN ({','.join('?' for _ in NEGATIVE_FEEDBACK_RATINGS)})
            ORDER BY f.ts DESC, f.id DESC
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
              AND f.resolves_feedback_id IS NULL
            GROUP BY effective_query
            """,
            (since, root_text),
        )
        (
            unresolved_negative_feedback,
            resolved_negative_feedback,
            explicit_resolutions,
        ) = _split_resolved_feedback(
            live_recent_negative_feedback,
            live_positive_feedback_queries,
        )
        unresolved_verified_or_direct_negative_feedback = [
            row
            for row in unresolved_negative_feedback
            if row["linkage_quality"] in {"verified_event", "direct_query"}
            and bool(row["replayable_search_intent"])
            and not _is_probe_query(row["effective_query"])
        ]
        unresolved_negative_with_expected_target = [
            row
            for row in unresolved_verified_or_direct_negative_feedback
            if str(row.get("expected_artifact_id") or "").strip()
        ]
        unresolved_negative_without_expected_target = [
            row
            for row in unresolved_verified_or_direct_negative_feedback
            if not str(row.get("expected_artifact_id") or "").strip()
        ]
        search_issue_candidates = unresolved_verified_or_direct_negative_feedback[:limit]
        route_outcomes = _rows(
            conn,
            """
            SELECT route_outcome, COUNT(*) AS count, MAX(ts) AS last_seen
            FROM usage_events
            WHERE ts >= ? AND root = ? AND tool = 'knowledge_search'
              AND success = 1 AND route_outcome <> 'none'
            GROUP BY route_outcome
            ORDER BY count DESC, route_outcome
            """,
            (since, root_text),
        )
        route_verification_failures = _rows(
            conn,
            """
            SELECT id AS usage_event_id, ts, query, artifact_type,
                   route_feedback_id, route_artifact_id, route_outcome
            FROM usage_events
            WHERE ts >= ? AND root = ? AND tool = 'knowledge_search'
              AND success = 1 AND route_outcome = 'verification_failed'
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (since, root_text, limit),
        )
        replay_ready_row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN f.rating = 'useful'
                              AND f.resolves_feedback_id IS NOT NULL
                              AND parent.id IS NOT NULL AND parent.root = f.root
                              AND parent.rating IN (
                                  'missing', 'noisy', 'not_useful', 'stale', 'wrong_artifact'
                              )
                              AND COALESCE(f.artifact_id, '') <> ''
                              AND f.linkage_status = 'verified_event'
                              AND e.id IS NOT NULL AND e.root = f.root
                              AND e.tool = 'knowledge_search' AND e.success = 1
                              AND COALESCE(f.query, '') = COALESCE(e.query, '')
                              AND json_valid(COALESCE(e.top_ids_json, '')) = 1
                              AND EXISTS (
                                  SELECT 1 FROM json_each(e.top_ids_json) AS result
                                  WHERE CAST(result.value AS TEXT) = f.artifact_id
                              )
                              AND (
                                  COALESCE(parent.expected_artifact_id, '') = ''
                                  OR parent.expected_artifact_id = f.artifact_id
                              )
                         THEN 1 ELSE 0 END) AS explicit_resolution,
                SUM(CASE WHEN f.rating = 'useful'
                              AND f.resolves_feedback_id IS NULL
                              AND COALESCE(f.artifact_id, '') <> ''
                              AND COALESCE(NULLIF(f.query, ''), e.query) IS NOT NULL
                              AND f.linkage_status = 'verified_event'
                              AND e.id IS NOT NULL AND e.root = f.root
                              AND e.tool = 'knowledge_search' AND e.success = 1
                              AND COALESCE(f.query, '') = COALESCE(e.query, '')
                              AND json_valid(COALESCE(e.top_ids_json, '')) = 1
                              AND EXISTS (
                                  SELECT 1 FROM json_each(e.top_ids_json) AS result
                                  WHERE CAST(result.value AS TEXT) = f.artifact_id
                              )
                         THEN 1 ELSE 0 END) AS verified_event,
                SUM(CASE WHEN f.rating = 'useful'
                              AND f.resolves_feedback_id IS NULL
                              AND COALESCE(f.artifact_id, '') <> ''
                              AND COALESCE(NULLIF(f.query, ''), e.query) IS NOT NULL
                              AND (
                                  (f.linkage_status = 'direct_query' AND f.event_id IS NULL)
                                  OR (
                                      f.linkage_status = 'legacy'
                                      AND (
                                          f.event_id IS NULL
                                          OR (
                                              e.id IS NOT NULL AND e.root = f.root
                                              AND e.tool = 'knowledge_search' AND e.success = 1
                                          )
                                      )
                                  )
                              )
                         THEN 1 ELSE 0 END) AS direct_or_legacy
            FROM feedback AS f
            LEFT JOIN usage_events AS e ON e.id = f.event_id
            LEFT JOIN feedback AS parent ON parent.id = f.resolves_feedback_id
            WHERE f.ts >= ? AND f.root = ?
            """,
            (since, root_text),
        ).fetchone()
        replay_ready_label_counts = {
            "explicit_resolution": int(replay_ready_row["explicit_resolution"] or 0),
            "verified_event": int(replay_ready_row["verified_event"] or 0),
            "direct_or_legacy": int(replay_ready_row["direct_or_legacy"] or 0),
        }
        replay_ready_label_counts["total"] = sum(replay_ready_label_counts.values())
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
    for row in search_issue_candidates[:limit]:
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
        "implicit_feedback_count": implicit_feedback_count,
        "live_implicit_feedback_count": live_implicit_feedback_count,
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
        "feedback_linkage_quality": feedback_linkage_quality,
        "feedback_linkage_counts": feedback_linkage_counts,
        "feedback_resolution_quality": feedback_resolution_quality,
        "feedback_resolution_counts": feedback_resolution_counts,
        "unresolved_negative_feedback": unresolved_negative_feedback,
        "unresolved_verified_or_direct_negative_feedback": (
            unresolved_verified_or_direct_negative_feedback[:limit]
        ),
        "unresolved_negative_with_expected_target": (
            unresolved_negative_with_expected_target[:limit]
        ),
        "unresolved_negative_without_expected_target": (
            unresolved_negative_without_expected_target[:limit]
        ),
        "resolved_negative_feedback": resolved_negative_feedback,
        "explicit_resolutions": explicit_resolutions[:limit],
        "explicit_resolution_count": feedback_resolution_counts.get("explicit_resolution", 0),
        "route_outcomes": route_outcomes,
        "route_verification_failures": route_verification_failures,
        "replay_ready_label_counts": replay_ready_label_counts,
        "search_issue_candidates": search_issue_candidates,
        "unresolved_negative_with_current_expected_target": [],
        "unresolved_negative_without_current_expected_target": search_issue_candidates,
        "behaviorally_resolved_negative_feedback": [],
        "correction_candidates": [],
        "latest_index_metadata": latest_index_rows[0] if latest_index_rows else None,
        "recent_builds": recent_builds,
        "improvement_candidates": improvement_candidates[:limit],
    }
