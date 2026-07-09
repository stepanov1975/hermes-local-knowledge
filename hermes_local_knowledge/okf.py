"""Privacy-safe queue/state helpers for generated tool OKF artifacts."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

QUEUE_DB_NAME = "okf_queue.sqlite"
OKF_WORKER_ENV = "HERMES_LOCAL_KNOWLEDGE_OKF_WORKER"
DEFAULT_MAX_ARG_ITEMS = 8
DEFAULT_MAX_ATTEMPTS = 3

_COLUMN_DEFINITIONS = {
    "tool_name": "TEXT PRIMARY KEY",
    "toolset": "TEXT",
    "schema_hash": "TEXT",
    "schema_json": "TEXT",
    "first_seen": "TEXT NOT NULL",
    "last_seen": "TEXT NOT NULL",
    "use_count": "INTEGER NOT NULL DEFAULT 0",
    "success_count": "INTEGER NOT NULL DEFAULT 0",
    "error_count": "INTEGER NOT NULL DEFAULT 0",
    "last_error_type": "TEXT",
    "last_error_message": "TEXT",
    "arg_shape_json": "TEXT NOT NULL DEFAULT '{}'",
    "status": "TEXT NOT NULL DEFAULT 'pending'",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "claimed_at": "TEXT",
    "claim_token": "TEXT",
    "okf_path": "TEXT",
    "last_attempt_error": "TEXT",
}

_MIGRATION_COLUMN_DEFINITIONS = {
    "toolset": "TEXT",
    "schema_hash": "TEXT",
    "schema_json": "TEXT",
    "first_seen": "TEXT",
    "last_seen": "TEXT",
    "use_count": "INTEGER DEFAULT 0",
    "success_count": "INTEGER DEFAULT 0",
    "error_count": "INTEGER DEFAULT 0",
    "last_error_type": "TEXT",
    "last_error_message": "TEXT",
    "arg_shape_json": "TEXT DEFAULT '{}'",
    "status": "TEXT DEFAULT 'pending'",
    "attempt_count": "INTEGER DEFAULT 0",
    "claimed_at": "TEXT",
    "claim_token": "TEXT",
    "okf_path": "TEXT",
    "last_attempt_error": "TEXT",
}

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|bearer)\b\s*[:=]\s*\S+"
)
_SECRET_WORD = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|bearer)\b")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def okf_queue_db_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / QUEUE_DB_NAME


def okf_dir(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / "okfs" / "tools"


def worker_lock_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / "okf_worker.lock"


def schema_hash(schema: Mapping[str, Any] | None) -> str:
    canonical = json.dumps(schema or {}, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_schema_json(schema: Mapping[str, Any] | None) -> str:
    return json.dumps(schema or {}, sort_keys=True, separators=(",", ":"), default=str)


def safe_arg_shape(value: Any, *, max_items: int = DEFAULT_MAX_ARG_ITEMS, depth: int = 0) -> dict[str, Any]:
    """Return argument structure without scalar values.

    Tool arguments can contain document text, chat contents, email bodies, paths,
    tokens, or other private data. The OKF queue needs routing shape only, so this
    function records coarse value types and bounded item counts without scalar
    values or raw mapping keys.
    """
    if depth >= 6:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, Mapping):
        items = list(value.items())
        shaped: dict[str, Any] = {}
        for index, (_raw_key, raw_child) in enumerate(items[:max_items]):
            shaped[f"field_{index}"] = safe_arg_shape(raw_child, max_items=max_items, depth=depth + 1)
        result: dict[str, Any] = {"type": "object", "field_count": len(items), "fields": shaped}
        if len(items) > max_items:
            result["truncated"] = True
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = list(value)
        result = {
            "type": "array",
            "length": len(values),
            "items": [safe_arg_shape(item, max_items=max_items, depth=depth + 1) for item in values[:max_items]],
        }
        if len(values) > max_items:
            result["truncated"] = True
        return result
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "bool"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "int"}
    if isinstance(value, float):
        return {"type": "float"}
    if isinstance(value, str):
        return {"type": "str"}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "bytes"}
    return {"type": type(value).__name__}


def _sanitize_snippet(value: Any, *, max_chars: int = 240) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).replace("\x00", "").strip()
    if not text:
        return None
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    if _SECRET_WORD.search(text) and len(text) > max_chars:
        return "<redacted secret-like message>"
    return text[:max_chars]


def _safe_error_type(value: Any) -> str | None:
    text = _sanitize_snippet(value, max_chars=80)
    if not text:
        return None
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", text):
        return text
    return "Error"


def _redacted_error_marker(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return "<redacted>"


def _connect(state_dir: Path) -> sqlite3.Connection:
    state_dir.expanduser().resolve().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(okf_queue_db_path(state_dir))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    columns_sql = ",\n      ".join(f"{name} {definition}" for name, definition in _COLUMN_DEFINITIONS.items())
    conn.execute(f"CREATE TABLE IF NOT EXISTS okf_candidates (\n      {columns_sql}\n    )")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(okf_candidates)")}
    if "tool_name" not in existing:
        raise RuntimeError("okf_candidates exists without required tool_name primary key")
    for name in _COLUMN_DEFINITIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE okf_candidates ADD COLUMN {name} {_MIGRATION_COLUMN_DEFINITIONS[name]}")
    conn.execute("UPDATE okf_candidates SET use_count = 0 WHERE use_count IS NULL")
    conn.execute("UPDATE okf_candidates SET success_count = 0 WHERE success_count IS NULL")
    conn.execute("UPDATE okf_candidates SET error_count = 0 WHERE error_count IS NULL")
    conn.execute("UPDATE okf_candidates SET attempt_count = 0 WHERE attempt_count IS NULL")
    conn.execute("UPDATE okf_candidates SET arg_shape_json = '{}' WHERE arg_shape_json IS NULL")
    conn.execute("UPDATE okf_candidates SET status = 'pending' WHERE status IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_okf_candidates_status_seen ON okf_candidates(status, use_count, last_seen)")
    conn.commit()


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def upsert_tool_candidate(
    state_dir: Path,
    *,
    tool_name: str,
    toolset: str | None,
    schema: Mapping[str, Any] | None,
    args: Any,
    success: bool | None = True,
    error_type: str | None = None,
    error_message: str | None = None,
    now: str | None = None,
) -> None:
    if not tool_name:
        return
    timestamp = now or utc_now()
    schema_json = canonical_schema_json(schema)
    digest = schema_hash(schema)
    arg_shape_json = json.dumps(safe_arg_shape(args), sort_keys=True, separators=(",", ":"))
    success_increment = 1 if success is not False else 0
    error_increment = 1 if success is False else 0
    clean_error_type = _safe_error_type(error_type)
    clean_error_message = _redacted_error_marker(error_message)
    with _connect(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO okf_candidates (
              tool_name, toolset, schema_hash, schema_json, first_seen, last_seen,
              use_count, success_count, error_count, last_error_type,
              last_error_message, arg_shape_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(tool_name) DO UPDATE SET
              toolset=excluded.toolset,
              schema_hash=excluded.schema_hash,
              schema_json=excluded.schema_json,
              last_seen=excluded.last_seen,
              use_count=okf_candidates.use_count + 1,
              success_count=okf_candidates.success_count + excluded.success_count,
              error_count=okf_candidates.error_count + excluded.error_count,
              last_error_type=COALESCE(excluded.last_error_type, okf_candidates.last_error_type),
              last_error_message=COALESCE(excluded.last_error_message, okf_candidates.last_error_message),
              arg_shape_json=excluded.arg_shape_json,
              status=CASE
                WHEN okf_candidates.schema_hash != excluded.schema_hash THEN 'pending'
                ELSE okf_candidates.status
              END,
              claimed_at=CASE
                WHEN okf_candidates.schema_hash != excluded.schema_hash THEN NULL
                ELSE okf_candidates.claimed_at
              END,
              claim_token=CASE
                WHEN okf_candidates.schema_hash != excluded.schema_hash THEN NULL
                ELSE okf_candidates.claim_token
              END,
              okf_path=CASE
                WHEN okf_candidates.schema_hash != excluded.schema_hash THEN NULL
                ELSE okf_candidates.okf_path
              END
            """,
            (
                tool_name,
                toolset,
                digest,
                schema_json,
                timestamp,
                timestamp,
                success_increment,
                error_increment,
                clean_error_type,
                clean_error_message,
                arg_shape_json,
            ),
        )


def pending_candidates(state_dir: Path, *, limit: int = 10, min_use_count: int = 1) -> list[dict[str, Any]]:
    with _connect(state_dir) as conn:
        rows = conn.execute(
            """
            SELECT * FROM okf_candidates
            WHERE status = 'pending' AND use_count >= ?
            ORDER BY use_count DESC, last_seen ASC, tool_name ASC
            LIMIT ?
            """,
            (min_use_count, limit),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def claim_candidates(
    state_dir: Path,
    *,
    limit: int,
    min_use_count: int = 1,
    stale_after_seconds: int = 600,
    claim_token: str | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    token = claim_token or uuid.uuid4().hex
    timestamp = now or utc_now()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with _connect(state_dir) as conn:
        rows = conn.execute(
            """
            SELECT tool_name FROM okf_candidates
            WHERE use_count >= ?
              AND (status = 'pending' OR (status = 'claimed' AND claimed_at < ?))
            ORDER BY use_count DESC, last_seen ASC, tool_name ASC
            LIMIT ?
            """,
            (min_use_count, cutoff, limit),
        ).fetchall()
        names = [str(row["tool_name"]) for row in rows]
        if names:
            conn.executemany(
                """
                UPDATE okf_candidates
                SET status = 'claimed', claimed_at = ?, claim_token = ?, attempt_count = attempt_count + 1,
                    last_attempt_error = NULL
                WHERE tool_name = ?
                """,
                [(timestamp, token, name) for name in names],
            )
        claimed = conn.execute(
            f"SELECT * FROM okf_candidates WHERE claim_token = ? AND tool_name IN ({','.join('?' for _ in names)})"
            if names
            else "SELECT * FROM okf_candidates WHERE 0",
            (token, *names) if names else (),
        ).fetchall()
    return [_row_dict(row) for row in claimed]


def mark_candidate_done(state_dir: Path, *, tool_name: str, claim_token: str, okf_path: Path) -> bool:
    with _connect(state_dir) as conn:
        cursor = conn.execute(
            """
            UPDATE okf_candidates
            SET status = 'done', okf_path = ?, claim_token = NULL, claimed_at = NULL,
                last_attempt_error = NULL
            WHERE tool_name = ? AND claim_token = ?
            """,
            (str(okf_path), tool_name, claim_token),
        )
        return cursor.rowcount == 1


def mark_candidate_error(
    state_dir: Path,
    *,
    tool_name: str,
    claim_token: str,
    error: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    clean_error = _redacted_error_marker(error)
    with _connect(state_dir) as conn:
        cursor = conn.execute(
            """
            UPDATE okf_candidates
            SET status = CASE WHEN attempt_count >= ? THEN 'error' ELSE 'pending' END,
                claim_token = NULL,
                claimed_at = NULL,
                last_attempt_error = ?
            WHERE tool_name = ? AND claim_token = ?
            """,
            (max_attempts, clean_error, tool_name, claim_token),
        )
        return cursor.rowcount == 1


def queue_counts(state_dir: Path) -> dict[str, int]:
    with _connect(state_dir) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM okf_candidates GROUP BY status").fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}
