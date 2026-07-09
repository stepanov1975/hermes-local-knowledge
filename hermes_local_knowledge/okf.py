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

from .paths import path_is_relative_to
from .text_utils import parse_frontmatter, safe_read_text, slugify

QUEUE_DB_NAME = "okf_queue.sqlite"
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
_EMAIL_ADDRESS = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SCHEMA_VALUE_KEYS = {
    "$comment",
    "const",
    "default",
    "description",
    "enum",
    "example",
    "examples",
    "markdownDescription",
    "summary",
    "title",
}
_GENERIC_ROUTING_PHRASES = {"okf", "placeholder", "tbd", "todo", "tool", "x"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def okf_queue_db_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / QUEUE_DB_NAME


def okf_dir(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / "okfs" / "tools"


def okf_file_path(state_dir: Path, tool_name: str) -> Path:
    return okf_dir(state_dir) / f"{slugify(tool_name)}.md"


def generation_lock_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / "okf_generation.lock"


def worker_lock_path(state_dir: Path) -> Path:
    """Compatibility alias for the pre-0.3.1 subprocess implementation."""

    return generation_lock_path(state_dir)


def schema_hash(schema: Mapping[str, Any] | None) -> str:
    canonical = json.dumps(schema or {}, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _schema_string(value: Any, *, max_chars: int = 240) -> str:
    text = str(value).replace("\x00", "").strip()
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _EMAIL_ADDRESS.sub("<redacted-email>", text)
    if _SECRET_WORD.search(text):
        return "<redacted>"
    return text[:max_chars]


def safe_schema_view(value: Any, *, max_items: int = DEFAULT_MAX_ARG_ITEMS * 2, depth: int = 0) -> Any:
    """Return schema structure without example/default/private scalar values."""

    if depth >= 8:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, Mapping):
        items = list(value.items())
        shaped: dict[str, Any] = {}
        for raw_key, raw_child in items[:max_items]:
            key = str(raw_key).strip()[:120]
            if key in _SCHEMA_VALUE_KEYS:
                entry: dict[str, Any] = {"redacted": True, "type": type(raw_child).__name__}
                if isinstance(raw_child, Sequence) and not isinstance(raw_child, (str, bytes, bytearray)):
                    entry["count"] = len(raw_child)
                shaped[key] = entry
                continue
            shaped[key] = safe_schema_view(raw_child, max_items=max_items, depth=depth + 1)
        if len(items) > max_items:
            shaped["truncated"] = True
            shaped["field_count"] = len(items)
        return shaped
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = list(value)
        shaped_list = [safe_schema_view(item, max_items=max_items, depth=depth + 1) for item in values[:max_items]]
        if len(values) > max_items:
            shaped_list.append({"truncated": True, "length": len(values)})
        return shaped_list
    if isinstance(value, str):
        return _schema_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def canonical_schema_json(schema: Mapping[str, Any] | None) -> str:
    return json.dumps(safe_schema_view(schema or {}), sort_keys=True, separators=(",", ":"), default=str)


def _safe_schema_from_json_text(schema_json: Any) -> Any:
    try:
        parsed = json.loads(str(schema_json or "{}"))
    except json.JSONDecodeError:
        return {}
    return safe_schema_view(parsed)


def _safe_schema_json_text(schema_json: Any) -> str:
    return json.dumps(_safe_schema_from_json_text(schema_json), sort_keys=True, separators=(",", ":"), default=str)


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


def _safe_arg_shape_from_json_text(arg_shape_json: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(arg_shape_json or "{}"))
    except json.JSONDecodeError:
        return {}
    if _is_canonical_arg_shape(parsed):
        return parsed
    return safe_arg_shape(parsed)


def _is_canonical_arg_shape(value: Any) -> bool:
    if not isinstance(value, Mapping) or not isinstance(value.get("type"), str):
        return False
    shape_type = value["type"]
    if shape_type == "object":
        if not set(value).issubset({"type", "field_count", "fields", "truncated"}):
            return False
        field_count = value.get("field_count")
        fields = value.get("fields")
        if (
            not isinstance(field_count, int)
            or isinstance(field_count, bool)
            or field_count < 0
            or not isinstance(fields, Mapping)
            or len(fields) > DEFAULT_MAX_ARG_ITEMS
        ):
            return False
        expected_keys = [f"field_{index}" for index in range(len(fields))]
        if list(fields) != expected_keys or not all(_is_canonical_arg_shape(child) for child in fields.values()):
            return False
        if "truncated" in value:
            return value.get("truncated") is True and field_count > len(fields)
        return field_count == len(fields)
    if shape_type == "array":
        if not set(value).issubset({"type", "length", "items", "truncated"}):
            return False
        length = value.get("length")
        items = value.get("items")
        if (
            not isinstance(length, int)
            or isinstance(length, bool)
            or length < 0
            or not isinstance(items, list)
            or len(items) > DEFAULT_MAX_ARG_ITEMS
            or not all(_is_canonical_arg_shape(child) for child in items)
        ):
            return False
        if "truncated" in value:
            return value.get("truncated") is True and length > len(items)
        return length == len(items)
    if not set(value).issubset({"type", "truncated"}):
        return False
    if "truncated" not in value:
        return shape_type in {"null", "bool", "int", "float", "str", "bytes"}
    return value.get("truncated") is True and shape_type in {
        "dict",
        "list",
        "str",
        "int",
        "float",
        "bool",
        "NoneType",
        "bytes",
        "bytearray",
    }


def _safe_arg_shape_json_text(arg_shape_json: Any) -> str:
    return json.dumps(_safe_arg_shape_from_json_text(arg_shape_json), sort_keys=True, separators=(",", ":"))


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
    for row in conn.execute("SELECT tool_name, schema_json FROM okf_candidates WHERE schema_json IS NOT NULL").fetchall():
        safe_schema_json = _safe_schema_json_text(row["schema_json"])
        if safe_schema_json != row["schema_json"]:
            conn.execute(
                "UPDATE okf_candidates SET schema_json = ? WHERE tool_name = ?",
                (safe_schema_json, row["tool_name"]),
            )
    for row in conn.execute("SELECT tool_name, arg_shape_json FROM okf_candidates WHERE arg_shape_json IS NOT NULL").fetchall():
        safe_arg_shape_json = _safe_arg_shape_json_text(row["arg_shape_json"])
        if safe_arg_shape_json != row["arg_shape_json"]:
            conn.execute(
                "UPDATE okf_candidates SET arg_shape_json = ? WHERE tool_name = ?",
                (safe_arg_shape_json, row["tool_name"]),
            )
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
              END,
              attempt_count=CASE
                WHEN okf_candidates.schema_hash != excluded.schema_hash THEN 0
                ELSE okf_candidates.attempt_count
              END,
              last_attempt_error=CASE
                WHEN okf_candidates.schema_hash != excluded.schema_hash THEN NULL
                ELSE okf_candidates.last_attempt_error
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
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    claim_token: str | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    token = claim_token or uuid.uuid4().hex
    timestamp = now or utc_now()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with _connect(state_dir) as conn:
        conn.execute(
            """
            UPDATE okf_candidates
            SET status = 'error', claim_token = NULL, claimed_at = NULL,
                last_attempt_error = '<redacted>'
            WHERE attempt_count >= ?
              AND (status = 'pending' OR (status = 'claimed' AND claimed_at < ?))
            """,
            (max_attempts, cutoff),
        )
        rows = conn.execute(
            """
            SELECT tool_name FROM okf_candidates
            WHERE use_count >= ?
              AND attempt_count < ?
              AND (status = 'pending' OR (status = 'claimed' AND claimed_at < ?))
            ORDER BY use_count DESC, last_seen ASC, tool_name ASC
            LIMIT ?
            """,
            (min_use_count, max_attempts, cutoff, limit),
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


def recover_stale_claims(
    state_dir: Path,
    *,
    stale_after_seconds: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: str | None = None,
) -> int:
    current = (
        datetime.fromisoformat(now.replace("Z", "+00:00"))
        if now is not None
        else datetime.now(timezone.utc)
    )
    cutoff = (current - timedelta(seconds=stale_after_seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with _connect(state_dir) as conn:
        cursor = conn.execute(
            """
            UPDATE okf_candidates
            SET status = CASE WHEN attempt_count >= ? THEN 'error' ELSE 'pending' END,
                claim_token = NULL,
                claimed_at = NULL,
                last_attempt_error = '<redacted>'
            WHERE status = 'claimed' AND claimed_at < ?
            """,
            (max_attempts, cutoff),
        )
        return cursor.rowcount


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


def _json_field(row: Mapping[str, Any], field: str) -> Any:
    try:
        return json.loads(str(row.get(field) or "{}"))
    except json.JSONDecodeError:
        return {}


def candidate_packet(row: Mapping[str, Any], state_dir: Path) -> dict[str, Any]:
    """Return the privacy-safe packet a worker may use to author an OKF."""

    tool_name = str(row.get("tool_name") or "")
    return {
        "tool": tool_name,
        "tool_name": tool_name,
        "toolset": row.get("toolset"),
        "schema_hash": row.get("schema_hash"),
        "schema": _safe_schema_from_json_text(row.get("schema_json")),
        "arg_shape": _safe_arg_shape_from_json_text(row.get("arg_shape_json")),
        "use_count": int(row.get("use_count") or 0),
        "success_count": int(row.get("success_count") or 0),
        "error_count": int(row.get("error_count") or 0),
        "last_error_type": row.get("last_error_type"),
        "last_error_message": row.get("last_error_message"),
        "claim_token": row.get("claim_token"),
        "target_path": str(okf_file_path(state_dir, tool_name)),
    }


def claimed_candidate(state_dir: Path, *, tool_name: str, claim_token: str) -> dict[str, Any] | None:
    with _connect(state_dir) as conn:
        row = conn.execute(
            "SELECT * FROM okf_candidates WHERE tool_name = ? AND claim_token = ? AND status = 'claimed'",
            (tool_name, claim_token),
        ).fetchone()
    return _row_dict(row) if row else None


def _frontmatter_list(frontmatter: Mapping[str, Any], key: str) -> list[str]:
    value = frontmatter.get(key)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _useful_routing_phrase(phrase: str, *, tool_name: str) -> bool:
    text = phrase.strip().lower()
    if len(text) < 8 or text in _GENERIC_ROUTING_PHRASES:
        return False
    tokens = {token for token in re.findall(r"[a-z0-9]{2,}", text) if token not in _GENERIC_ROUTING_PHRASES}
    if len(tokens) < 2:
        return False
    tool_tokens = {token for token in re.findall(r"[a-z0-9]{2,}", tool_name.lower())}
    return not tokens.issubset(tool_tokens)


def validate_okf_file(state_dir: Path, *, claim_token: str, path: Path) -> dict[str, Any]:
    """Validate a worker-authored OKF before marking a candidate done."""

    errors: list[str] = []
    resolved_state_dir = state_dir.expanduser().resolve()
    allowed_root = okf_dir(resolved_state_dir)
    resolved_path = path.expanduser().resolve()
    if not path_is_relative_to(resolved_path, allowed_root):
        errors.append(f"path must be under {allowed_root}")
    if resolved_path.suffix != ".md":
        errors.append("OKF path must use .md suffix")
    text = safe_read_text(resolved_path, max_chars=80_000)
    if not text:
        errors.append("OKF file is missing or empty")
    if _SECRET_ASSIGNMENT.search(text):
        errors.append("OKF file contains secret-like assignment text")
    frontmatter = parse_frontmatter(text)
    artifact_type = str(frontmatter.get("artifact_type") or "").strip()
    if artifact_type != "tool_okf":
        errors.append("frontmatter artifact_type must be tool_okf")
    tool_name = str(frontmatter.get("tool") or "").strip()
    if not tool_name:
        errors.append("frontmatter tool is required")
    schema_digest = str(frontmatter.get("schema_hash") or "").strip()
    if not schema_digest:
        errors.append("frontmatter schema_hash is required")
    aliases = _frontmatter_list(frontmatter, "aliases")
    triggers = _frontmatter_list(frontmatter, "triggers")
    if not any(_useful_routing_phrase(phrase, tool_name=tool_name) for phrase in [*aliases, *triggers]):
        errors.append("frontmatter aliases or triggers must include at least one specific multi-word routing phrase")

    row = claimed_candidate(resolved_state_dir, tool_name=tool_name, claim_token=claim_token) if tool_name else None
    if row is None:
        errors.append("no claimed candidate matches the provided claim token and tool")
    else:
        expected_path = okf_file_path(resolved_state_dir, tool_name).resolve()
        if resolved_path != expected_path:
            errors.append(f"path must match claimed target path {expected_path}")
        if schema_digest != str(row.get("schema_hash") or ""):
            errors.append("frontmatter schema_hash does not match claimed candidate")

    return {
        "valid": not errors,
        "errors": errors,
        "tool": tool_name,
        "path": str(resolved_path),
        "claim_token": claim_token,
    }
