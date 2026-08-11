"""Privacy-safe queue/state helpers for generated tool OKF artifacts."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config, resolve_config
from .implicit import on_post_tool_call as _on_post_tool_call_implicit_feedback

logger = logging.getLogger(__name__)

QUEUE_DB_NAME = "okf_queue.sqlite"
INDEX_DIRTY_MARKER_NAME = "okf_index_dirty"
OKF_GENERATOR_VERSION = "3"
DEFAULT_MAX_ARG_ITEMS = 8
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_RELATED_TOOLS = 32
GENERATION_LEASE_NAME = "okf_generation"
OKF_WORKER_ENV = "HERMES_LOCAL_KNOWLEDGE_OKF_WORKER"
_MAX_SCHEMA_ITEMS = DEFAULT_MAX_ARG_ITEMS * 2
_MAX_SCHEMA_DEPTH = 8

_COLUMN_DEFINITIONS = {
    "tool_name": "TEXT PRIMARY KEY",
    "toolset": "TEXT",
    "schema_hash": "TEXT",
    "schema_json": "TEXT",
    "generator_version": "TEXT",
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
    "claim_generator_version": "TEXT",
    "related_tools_json": "TEXT NOT NULL DEFAULT '[]'",
    "okf_path": "TEXT",
    "last_attempt_error": "TEXT",
}

_CANDIDATE_COLUMNS = tuple(_COLUMN_DEFINITIONS)
_CANDIDATE_SELECT = ", ".join(
    (
        "tool_name",
        "toolset",
        "schema_hash",
        "COALESCE(schema_json, '{}') AS schema_json",
        "generator_version",
        "COALESCE(first_seen, '') AS first_seen",
        "COALESCE(last_seen, '') AS last_seen",
        "COALESCE(use_count, 0) AS use_count",
        "COALESCE(success_count, 0) AS success_count",
        "COALESCE(error_count, 0) AS error_count",
        "last_error_type",
        "last_error_message",
        "COALESCE(arg_shape_json, '{}') AS arg_shape_json",
        "COALESCE(status, 'pending') AS status",
        "COALESCE(attempt_count, 0) AS attempt_count",
        "claimed_at",
        "claim_token",
        "claim_generator_version",
        "COALESCE(related_tools_json, '[]') AS related_tools_json",
        "okf_path",
        "last_attempt_error",
    )
)

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
_EPHEMERAL_OKF_TEXT = re.compile(
    r"(?i)\b(?:use_count|success_count|error_count|last_error_type|observed counters?)\b"
)
_GENERIC_NEGATIVE_GUIDANCE = re.compile(
    r"(?i)\b(?:credentials?|secret values?|raw transcripts?|raw tool outputs?)\b"
)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "artifact"


def _safe_read_text(path: Path, *, max_chars: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(max_chars)
    except OSError:
        return ""


def _parse_bracket_list(value: str) -> list[str]:
    clean = value.strip()
    if clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1]
    return [item.strip().strip("'\"") for item in clean.split(",") if item.strip().strip("'\"")]


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    frontmatter: dict[str, Any] = {}
    current_key: str | None = None
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped or stripped.startswith("#"):
            continue
        list_item = re.match(r"^[-*]\s+(.+)$", stripped)
        if list_item and current_key:
            current_value = frontmatter.get(current_key)
            if isinstance(current_value, list):
                current_value.append(list_item.group(1).strip().strip("'\""))
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", stripped)
        if not match:
            continue
        key, value = match.groups()
        current_key = key
        value = value.strip()
        if not value:
            frontmatter[key] = []
        elif value.startswith("[") and value.endswith("]"):
            frontmatter[key] = _parse_bracket_list(value)
        else:
            frontmatter[key] = value.strip("'\"")
    return frontmatter


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def okf_queue_db_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / QUEUE_DB_NAME


def index_dirty_marker_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / INDEX_DIRTY_MARKER_NAME


def mark_index_dirty(state_dir: Path) -> None:
    marker = index_dirty_marker_path(state_dir)
    marker.mkdir(parents=True, exist_ok=True)
    (marker / uuid.uuid4().hex).touch(exist_ok=False)


def index_dirty_tokens(state_dir: Path) -> tuple[Path, ...]:
    marker = index_dirty_marker_path(state_dir)
    if not marker.is_dir():
        return ()
    return tuple(path for path in marker.iterdir() if path.is_file())


def okf_dir(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / "okfs" / "tools"


def okf_file_path(state_dir: Path, tool_name: str) -> Path:
    return okf_dir(state_dir) / f"{_slugify(tool_name)}.md"


def generation_lease_seconds(max_generation_seconds: int) -> int:
    """Return the fixed worker lease and stale-claim duration."""

    return max(300, 2 * max(1, int(max_generation_seconds)) + 120)


def schema_hash(schema: Mapping[str, Any] | None) -> str:
    canonical = json.dumps(schema or {}, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_SCHEMA_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}
_SCHEMA_BOOL_KEYS = {
    "additionalProperties",
    "nullable",
    "readOnly",
    "unevaluatedProperties",
    "uniqueItems",
    "writeOnly",
}
_SCHEMA_SINGLE_KEYS = {
    "additionalProperties",
    "contains",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedProperties",
}
_SCHEMA_BRANCH_KEYS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_PROPERTY_KEYS = {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
_SCHEMA_PROJECTED_KEYS = {
    "type",
    "required",
    "truncated",
    "property_count",
    "required_count",
    "branch_count",
    *_SCHEMA_BOOL_KEYS,
    *_SCHEMA_SINGLE_KEYS,
    *_SCHEMA_BRANCH_KEYS,
    *_SCHEMA_PROPERTY_KEYS,
}
_LEGACY_SCHEMA_ONLY_KEYS = frozenset(
    (
        _SCHEMA_VALUE_KEYS
        | {
            "$anchor",
            "$dynamicAnchor",
            "$dynamicRef",
            "$id",
            "$recursiveAnchor",
            "$recursiveRef",
            "$ref",
            "$schema",
            "$vocabulary",
            "additionalItems",
            "contentEncoding",
            "contentMediaType",
            "contentSchema",
            "dependencies",
            "dependentRequired",
            "deprecated",
            "discriminator",
            "exclusiveMaximum",
            "exclusiveMinimum",
            "externalDocs",
            "format",
            "id",
            "maximum",
            "maxContains",
            "maxItems",
            "maxLength",
            "maxProperties",
            "minimum",
            "minContains",
            "minItems",
            "minLength",
            "minProperties",
            "multipleOf",
            "pattern",
            "unevaluatedItems",
            "xml",
        }
    )
    - _SCHEMA_PROJECTED_KEYS
)


def _schema_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.replace("\x00", "").strip()
    return name if name and len(name) <= 120 else None


def project_routing_schema(value: Any, *, _depth: int = 0) -> dict[str, Any]:
    """Project a raw tool schema to bounded structural routing identity."""

    if not isinstance(value, Mapping):
        return {}
    if _depth >= _MAX_SCHEMA_DEPTH:
        return {"truncated": True}

    projected: dict[str, Any] = {}
    raw_type = value.get("type")
    if isinstance(raw_type, str) and raw_type in _SCHEMA_TYPES:
        projected["type"] = raw_type
    elif isinstance(raw_type, Sequence) and not isinstance(raw_type, (str, bytes, bytearray)):
        types = list(dict.fromkeys(item for item in raw_type if isinstance(item, str) and item in _SCHEMA_TYPES))
        if types:
            projected["type"] = types[:_MAX_SCHEMA_ITEMS]

    for key in _SCHEMA_PROPERTY_KEYS:
        raw_properties = value.get(key)
        if not isinstance(raw_properties, Mapping):
            continue
        properties: dict[str, Any] = {}
        for raw_name, child in list(raw_properties.items())[:_MAX_SCHEMA_ITEMS]:
            name = _schema_name(raw_name)
            if name is not None:
                properties[name] = project_routing_schema(child, _depth=_depth + 1)
        projected[key] = properties
        if len(raw_properties) > _MAX_SCHEMA_ITEMS:
            projected["truncated"] = True
            projected["property_count"] = len(raw_properties)

    raw_required = value.get("required")
    if isinstance(raw_required, Sequence) and not isinstance(raw_required, (str, bytes, bytearray)):
        required = list(
            dict.fromkeys(name for item in raw_required if (name := _schema_name(item)) is not None)
        )
        projected["required"] = required[:_MAX_SCHEMA_ITEMS]
        if len(required) > _MAX_SCHEMA_ITEMS:
            projected["truncated"] = True
            projected["required_count"] = len(required)

    for key in _SCHEMA_BOOL_KEYS:
        raw_flag = value.get(key)
        if isinstance(raw_flag, bool):
            projected[key] = raw_flag

    for key in _SCHEMA_SINGLE_KEYS:
        raw_child = value.get(key)
        if isinstance(raw_child, Mapping):
            projected[key] = project_routing_schema(raw_child, _depth=_depth + 1)

    for key in _SCHEMA_BRANCH_KEYS:
        raw_branches = value.get(key)
        if not isinstance(raw_branches, Sequence) or isinstance(raw_branches, (str, bytes, bytearray)):
            continue
        branches = [
            project_routing_schema(branch, _depth=_depth + 1)
            for branch in list(raw_branches)[:_MAX_SCHEMA_ITEMS]
            if isinstance(branch, Mapping)
        ]
        projected[key] = branches
        if len(raw_branches) > _MAX_SCHEMA_ITEMS:
            projected["truncated"] = True
            projected["branch_count"] = len(raw_branches)
    return projected


def is_routing_schema_projection(value: Any, *, _depth: int = 0) -> bool:
    """Return whether *value* is an admitted routing-schema projection."""

    if not isinstance(value, Mapping) or _depth > _MAX_SCHEMA_DEPTH:
        return False
    if not set(value).issubset(_SCHEMA_PROJECTED_KEYS):
        return False
    raw_type = value.get("type")
    if raw_type is not None:
        if isinstance(raw_type, str):
            if raw_type not in _SCHEMA_TYPES:
                return False
        else:
            if not (
                isinstance(raw_type, list)
                and 0 < len(raw_type) <= _MAX_SCHEMA_ITEMS
                and all(isinstance(item, str) and item in _SCHEMA_TYPES for item in raw_type)
            ):
                return False
            if len(set(raw_type)) != len(raw_type):
                return False
    for key in _SCHEMA_PROPERTY_KEYS:
        properties = value.get(key)
        if properties is None:
            continue
        if not isinstance(properties, Mapping) or len(properties) > _MAX_SCHEMA_ITEMS:
            return False
        if not all(
            _schema_name(name) == name and is_routing_schema_projection(child, _depth=_depth + 1)
            for name, child in properties.items()
        ):
            return False
    required = value.get("required")
    if required is not None:
        if not (
            isinstance(required, list)
            and len(required) <= _MAX_SCHEMA_ITEMS
            and all(_schema_name(name) == name for name in required)
        ):
            return False
        if len(set(required)) != len(required):
            return False
    for key in _SCHEMA_BOOL_KEYS:
        if key in value and not isinstance(value[key], bool) and key not in _SCHEMA_SINGLE_KEYS:
            return False
        if key in value and key in _SCHEMA_SINGLE_KEYS and not isinstance(value[key], (bool, Mapping)):
            return False
    for key in _SCHEMA_SINGLE_KEYS:
        if key not in value:
            continue
        child = value[key]
        if key in _SCHEMA_BOOL_KEYS and isinstance(child, bool):
            continue
        if not isinstance(child, Mapping) or not is_routing_schema_projection(child, _depth=_depth + 1):
            return False
    for key in _SCHEMA_BRANCH_KEYS:
        branches = value.get(key)
        if branches is not None and not (
            isinstance(branches, list)
            and len(branches) <= _MAX_SCHEMA_ITEMS
            and all(is_routing_schema_projection(branch, _depth=_depth + 1) for branch in branches)
        ):
            return False
    for key in ("property_count", "required_count", "branch_count"):
        count = value.get(key)
        if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count <= _MAX_SCHEMA_ITEMS):
            return False
    if "truncated" in value and value["truncated"] is not True:
        return False
    return True


def canonical_schema_json(schema: Mapping[str, Any] | None) -> str:
    return json.dumps(project_routing_schema(schema or {}), sort_keys=True, separators=(",", ":"))


def _has_legacy_schema_keyword(value: Any, *, _depth: int = 0) -> bool:
    """Recognize bounded v0.3.12-era schema nodes without treating property names as keywords."""

    if not isinstance(value, Mapping) or _depth >= _MAX_SCHEMA_DEPTH:
        return False
    items: list[tuple[Any, Any]] = []
    for index, item in enumerate(value.items()):
        if index >= _MAX_SCHEMA_ITEMS + 2:
            break
        items.append(item)
    if any(isinstance(key, str) and key in _LEGACY_SCHEMA_ONLY_KEYS for key, _child in items):
        return True
    for key, child in items:
        if key in _SCHEMA_PROPERTY_KEYS and isinstance(child, Mapping):
            for index, property_schema in enumerate(child.values()):
                if index >= _MAX_SCHEMA_ITEMS + 2:
                    break
                if _has_legacy_schema_keyword(property_schema, _depth=_depth + 1):
                    return True
        elif key in _SCHEMA_SINGLE_KEYS and isinstance(child, Mapping):
            if _has_legacy_schema_keyword(child, _depth=_depth + 1):
                return True
        elif key in _SCHEMA_BRANCH_KEYS and isinstance(child, Sequence) and not isinstance(
            child, (str, bytes, bytearray)
        ):
            for index, branch in enumerate(child):
                if index >= _MAX_SCHEMA_ITEMS + 1:
                    break
                if _has_legacy_schema_keyword(branch, _depth=_depth + 1):
                    return True
    return False


def _claim_schema_projection_json(schema_json: Any) -> str:
    """Return canonical current projection JSON for one selected claim row."""

    try:
        parsed = json.loads(str(schema_json or "{}"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored routing schema is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("stored routing schema is not a mapping")
    if is_routing_schema_projection(parsed):
        projection = dict(parsed)
    else:
        if not _has_legacy_schema_keyword(parsed):
            raise ValueError("stored routing schema is neither current nor recognized legacy data")
        projection = project_routing_schema(parsed)
        if not is_routing_schema_projection(projection):
            raise ValueError("legacy routing schema did not produce a valid projection")
    return json.dumps(projection, sort_keys=True, separators=(",", ":"))


def _stored_schema_projection(schema_json: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(schema_json or "{}"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored routing schema is not valid JSON") from exc
    if not isinstance(parsed, dict) or not is_routing_schema_projection(parsed):
        raise ValueError("stored routing schema is not a valid projection")
    return parsed


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


def _stored_arg_shape(arg_shape_json: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(arg_shape_json or "{}"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored argument shape is not valid JSON") from exc
    if not _is_canonical_arg_shape(parsed):
        raise ValueError("stored argument shape is not canonical")
    return dict(parsed)


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


@contextmanager
def _connect(state_dir: Path) -> Iterator[sqlite3.Connection]:
    state_dir.expanduser().resolve().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(okf_queue_db_path(state_dir))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        with conn:
            yield conn
    finally:
        conn.close()


def _candidate_table_columns(conn: sqlite3.Connection) -> set[str]:
    table_info = conn.execute("PRAGMA table_info(okf_candidates)").fetchall()
    if not table_info:
        return set()
    primary_key = [row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]]
    if primary_key != ["tool_name"]:
        raise RuntimeError("okf_candidates exists without required tool_name primary key")
    return {row[1] for row in table_info}


def _validate_lease_table(conn: sqlite3.Connection) -> None:
    table_info = conn.execute("PRAGMA table_info(okf_worker_leases)").fetchall()
    if not table_info:
        raise RuntimeError("okf_worker_leases is missing from the current OKF queue schema")
    primary_key = [row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]]
    if primary_key != ["name"]:
        raise RuntimeError("okf_worker_leases exists without required name primary key")
    columns = {row[1] for row in table_info}
    missing = {"name", "owner", "expires_at"} - columns
    if missing:
        raise RuntimeError(f"okf_worker_leases is missing required columns: {sorted(missing)}")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    existing = _candidate_table_columns(conn)
    if existing:
        missing = set(_CANDIDATE_COLUMNS) - existing
        if missing:
            raise RuntimeError(f"okf_candidates is missing required current columns: {sorted(missing)}")
        _validate_lease_table(conn)
        return

    columns_sql = ",\n      ".join(f"{name} {definition}" for name, definition in _COLUMN_DEFINITIONS.items())
    conn.execute(f"CREATE TABLE okf_candidates (\n      {columns_sql}\n    )")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_okf_candidates_status_seen ON okf_candidates(status, use_count, last_seen)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS okf_worker_leases (
          name TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          expires_at REAL NOT NULL
        )
        """
    )
    conn.commit()


def acquire_generation_lease(
    state_dir: Path,
    *,
    owner: str,
    lease_seconds: int,
    now: float | None = None,
) -> bool:
    """Acquire or replace an expired singleton lease for OKF generation."""

    if not owner:
        raise ValueError("generation lease owner must not be empty")
    current = time.time() if now is None else float(now)
    expires_at = current + max(1, int(lease_seconds))
    with _connect(state_dir) as conn:
        cursor = conn.execute(
            """
            INSERT INTO okf_worker_leases (name, owner, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              owner = excluded.owner,
              expires_at = excluded.expires_at
            WHERE okf_worker_leases.expires_at <= ?
            """,
            (GENERATION_LEASE_NAME, owner, expires_at, current),
        )
        return cursor.rowcount == 1


def release_generation_lease(state_dir: Path, *, owner: str) -> bool:
    """Release the generation lease only when *owner* still owns it."""

    with _connect(state_dir) as conn:
        cursor = conn.execute(
            "DELETE FROM okf_worker_leases WHERE name = ? AND owner = ?",
            (GENERATION_LEASE_NAME, owner),
        )
        return cursor.rowcount == 1


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
    del error_message
    timestamp = now or utc_now()
    schema_json = canonical_schema_json(schema)
    digest = schema_hash(schema)
    arg_shape_json = json.dumps(safe_arg_shape(args), sort_keys=True, separators=(",", ":"))
    success_increment = 1 if success is not False else 0
    error_increment = 1 if success is False else 0
    clean_error_type = _safe_error_type(error_type)
    with _connect(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO okf_candidates (
              tool_name, toolset, schema_hash, schema_json, generator_version, first_seen, last_seen,
              use_count, success_count, error_count, last_error_type,
              last_error_message, arg_shape_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, ?, 'pending')
            ON CONFLICT(tool_name) DO UPDATE SET
              toolset=excluded.toolset,
              schema_hash=excluded.schema_hash,
              schema_json=excluded.schema_json,
              generator_version=CASE
                WHEN NOT (okf_candidates.schema_hash IS excluded.schema_hash)
                  OR NOT (okf_candidates.toolset IS excluded.toolset)
                THEN excluded.generator_version
                WHEN COALESCE(okf_candidates.status, 'pending') = 'claimed'
                THEN okf_candidates.generator_version
                ELSE excluded.generator_version
              END,
              first_seen=COALESCE(okf_candidates.first_seen, excluded.first_seen),
              last_seen=excluded.last_seen,
              use_count=COALESCE(okf_candidates.use_count, 0) + 1,
              success_count=COALESCE(okf_candidates.success_count, 0) + excluded.success_count,
              error_count=COALESCE(okf_candidates.error_count, 0) + excluded.error_count,
              last_error_type=COALESCE(excluded.last_error_type, okf_candidates.last_error_type),
              arg_shape_json=excluded.arg_shape_json,
              status=CASE
                WHEN NOT (okf_candidates.schema_hash IS excluded.schema_hash)
                  OR NOT (okf_candidates.toolset IS excluded.toolset)
                  OR (
                    COALESCE(okf_candidates.status, 'pending') = 'done'
                    AND NOT (okf_candidates.generator_version IS excluded.generator_version)
                  )
                THEN 'pending'
                ELSE COALESCE(okf_candidates.status, 'pending')
              END,
              claimed_at=CASE
                WHEN NOT (okf_candidates.schema_hash IS excluded.schema_hash)
                  OR NOT (okf_candidates.toolset IS excluded.toolset)
                  OR (
                    COALESCE(okf_candidates.status, 'pending') = 'done'
                    AND NOT (okf_candidates.generator_version IS excluded.generator_version)
                  )
                THEN NULL
                ELSE okf_candidates.claimed_at
              END,
              claim_token=CASE
                WHEN NOT (okf_candidates.schema_hash IS excluded.schema_hash)
                  OR NOT (okf_candidates.toolset IS excluded.toolset)
                  OR (
                    COALESCE(okf_candidates.status, 'pending') = 'done'
                    AND NOT (okf_candidates.generator_version IS excluded.generator_version)
                  )
                THEN NULL
                ELSE okf_candidates.claim_token
              END,
              claim_generator_version=CASE
                WHEN NOT (okf_candidates.schema_hash IS excluded.schema_hash)
                  OR NOT (okf_candidates.toolset IS excluded.toolset)
                  OR (
                    COALESCE(okf_candidates.status, 'pending') = 'done'
                    AND NOT (okf_candidates.generator_version IS excluded.generator_version)
                  )
                THEN NULL
                ELSE okf_candidates.claim_generator_version
              END,
              related_tools_json=CASE
                WHEN NOT (okf_candidates.schema_hash IS excluded.schema_hash)
                  OR NOT (okf_candidates.toolset IS excluded.toolset)
                  OR (
                    COALESCE(okf_candidates.status, 'pending') = 'done'
                    AND NOT (okf_candidates.generator_version IS excluded.generator_version)
                  )
                THEN '[]'
                ELSE COALESCE(okf_candidates.related_tools_json, '[]')
              END,
              okf_path=CASE
                WHEN NOT (okf_candidates.schema_hash IS excluded.schema_hash)
                  OR NOT (okf_candidates.toolset IS excluded.toolset)
                  OR (
                    COALESCE(okf_candidates.status, 'pending') = 'done'
                    AND NOT (okf_candidates.generator_version IS excluded.generator_version)
                  )
                THEN NULL
                ELSE okf_candidates.okf_path
              END,
              attempt_count=CASE
                WHEN NOT (okf_candidates.schema_hash IS excluded.schema_hash)
                  OR NOT (okf_candidates.toolset IS excluded.toolset)
                  OR (
                    COALESCE(okf_candidates.status, 'pending') = 'done'
                    AND NOT (okf_candidates.generator_version IS excluded.generator_version)
                  )
                THEN 0
                ELSE COALESCE(okf_candidates.attempt_count, 0)
              END,
              last_attempt_error=CASE
                WHEN NOT (okf_candidates.schema_hash IS excluded.schema_hash)
                  OR NOT (okf_candidates.toolset IS excluded.toolset)
                  OR (
                    COALESCE(okf_candidates.status, 'pending') = 'done'
                    AND NOT (okf_candidates.generator_version IS excluded.generator_version)
                  )
                THEN NULL
                ELSE okf_candidates.last_attempt_error
              END
            """,
            (
                tool_name,
                toolset,
                digest,
                schema_json,
                OKF_GENERATOR_VERSION,
                timestamp,
                timestamp,
                success_increment,
                error_increment,
                clean_error_type,
                arg_shape_json,
            ),
        )


def pending_candidates(state_dir: Path, *, limit: int = 10, min_use_count: int = 1) -> list[dict[str, Any]]:
    with _connect(state_dir) as conn:
        rows = conn.execute(
            f"""
            SELECT {_CANDIDATE_SELECT} FROM okf_candidates
            WHERE COALESCE(status, 'pending') = 'pending' AND COALESCE(use_count, 0) >= ?
            ORDER BY COALESCE(use_count, 0) DESC, COALESCE(last_seen, '') ASC, tool_name ASC
            LIMIT ?
            """,
            (min_use_count, limit),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def has_generation_work(
    state_dir: Path,
    *,
    min_use_count: int,
    stale_after_seconds: int,
    now: str | None = None,
    timeout_seconds: float = 0.05,
) -> bool:
    """Return whether a worker can claim pending work or recover a stale claim."""

    current_dt = (
        datetime.fromisoformat(now.replace("Z", "+00:00"))
        if now is not None
        else datetime.now(timezone.utc)
    )
    cutoff = (current_dt - timedelta(seconds=stale_after_seconds)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    db_path = okf_queue_db_path(state_dir)
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout_seconds)
    except sqlite3.OperationalError:
        return False
    try:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(
            """
            SELECT CASE WHEN
              EXISTS (
                SELECT 1 FROM okf_candidates
                WHERE (COALESCE(status, 'pending') = 'pending' AND COALESCE(use_count, 0) >= ?)
                   OR (status = 'claimed' AND COALESCE(claimed_at, '') < ?)
                LIMIT 1
              )
              AND NOT EXISTS (
                SELECT 1 FROM okf_worker_leases
                WHERE name = ? AND expires_at > ?
              )
            THEN 1 ELSE 0 END
            """,
            (min_use_count, cutoff, GENERATION_LEASE_NAME, current_dt.timestamp()),
        ).fetchone()
        return row is not None and row[0] == 1
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def error_candidates(state_dir: Path, *, limit: int = 10) -> list[dict[str, Any]]:
    with _connect(state_dir) as conn:
        rows = conn.execute(
            f"""
            SELECT {_CANDIDATE_SELECT} FROM okf_candidates
            WHERE status = 'error'
            ORDER BY COALESCE(use_count, 0) DESC, COALESCE(last_seen, '') ASC, tool_name ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def retry_error_candidate(state_dir: Path, *, tool_name: str) -> bool:
    db_path = okf_queue_db_path(state_dir)
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=rw", uri=True)
    except sqlite3.OperationalError:
        if not db_path.is_file():
            return False
        raise
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT COALESCE(status, 'pending') AS status FROM okf_candidates WHERE tool_name = ?",
            (tool_name,),
        ).fetchone()
        if row is None or row["status"] != "error":
            conn.rollback()
            return False
        cursor = conn.execute(
            """
            UPDATE okf_candidates
            SET status = 'pending', attempt_count = 0, claimed_at = NULL,
                claim_token = NULL, claim_generator_version = NULL, related_tools_json = '[]',
                okf_path = NULL, last_attempt_error = NULL
            WHERE tool_name = ? AND status = 'error'
            """,
            (tool_name,),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _stale_claim_cutoff(*, stale_after_seconds: int, now: str | None = None) -> str:
    current = (
        datetime.fromisoformat(now.replace("Z", "+00:00"))
        if now is not None
        else datetime.now(timezone.utc)
    )
    return (current - timedelta(seconds=stale_after_seconds)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _recover_stale_claims_on_connection(
    conn: sqlite3.Connection,
    state_dir: Path,
    *,
    cutoff: str,
    max_attempts: int,
) -> int:
    stale_rows = conn.execute(
        f"SELECT {_CANDIDATE_SELECT} FROM okf_candidates "
        "WHERE status = 'claimed' AND COALESCE(claimed_at, '') < ?",
        (cutoff,),
    ).fetchall()
    completed = 0
    for stale_row in stale_rows:
        row = _row_dict(stale_row)
        tool_name = str(row.get("tool_name") or "")
        claim_token = str(row.get("claim_token") or "")
        path = okf_file_path(state_dir, tool_name)
        if path.is_file() and validate_okf_file(
            state_dir,
            claim_token=claim_token,
            path=path,
            _conn=conn,
        )["valid"]:
            completed += int(
                _mark_candidate_done_on_connection(
                    conn,
                    tool_name=tool_name,
                    claim_token=claim_token,
                    okf_path=path,
                )
            )
    if completed:
        mark_index_dirty(state_dir)
    cursor = conn.execute(
        """
        UPDATE okf_candidates
        SET status = CASE WHEN COALESCE(attempt_count, 0) >= ? THEN 'error' ELSE 'pending' END,
            generator_version = ?,
            claim_token = NULL,
            claimed_at = NULL,
            claim_generator_version = NULL,
            related_tools_json = '[]',
            okf_path = NULL,
            last_attempt_error = '<redacted>'
        WHERE status = 'claimed' AND COALESCE(claimed_at, '') < ?
        """,
        (max_attempts, OKF_GENERATOR_VERSION, cutoff),
    )
    return completed + cursor.rowcount


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
    cutoff = _stale_claim_cutoff(stale_after_seconds=stale_after_seconds, now=now)
    with _connect(state_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _recover_stale_claims_on_connection(
            conn,
            state_dir,
            cutoff=cutoff,
            max_attempts=max_attempts,
        )
        conn.execute(
            """
            UPDATE okf_candidates
            SET status = 'error', generator_version = ?, claim_token = NULL, claimed_at = NULL,
                claim_generator_version = NULL, related_tools_json = '[]',
                okf_path = NULL, last_attempt_error = '<redacted>'
            WHERE COALESCE(attempt_count, 0) >= ? AND COALESCE(status, 'pending') = 'pending'
            """,
            (OKF_GENERATOR_VERSION, max_attempts),
        )
        rows = conn.execute(
            f"""
            SELECT {_CANDIDATE_SELECT} FROM okf_candidates
            WHERE COALESCE(use_count, 0) >= ?
              AND COALESCE(attempt_count, 0) < ?
              AND COALESCE(status, 'pending') = 'pending'
            ORDER BY COALESCE(use_count, 0) DESC, COALESCE(last_seen, '') ASC, tool_name ASC
            LIMIT ?
            """,
            (min_use_count, max_attempts, limit),
        ).fetchall()
        schema_updates: list[tuple[str, str]] = []
        for row in rows:
            try:
                normalized_schema = _claim_schema_projection_json(row["schema_json"])
            except (TypeError, ValueError):
                continue
            if normalized_schema != str(row["schema_json"]):
                schema_updates.append((normalized_schema, str(row["tool_name"])))
        if schema_updates:
            conn.executemany(
                "UPDATE okf_candidates SET schema_json = ? "
                "WHERE tool_name = ? AND COALESCE(status, 'pending') = 'pending'",
                schema_updates,
            )
        names = [str(row["tool_name"]) for row in rows]
        if names:
            updates = [
                (
                    timestamp,
                    token,
                    OKF_GENERATOR_VERSION,
                    OKF_GENERATOR_VERSION,
                    json.dumps(
                        _allowed_related_tools_from_conn(conn, dict(row), limit=DEFAULT_MAX_RELATED_TOOLS),
                        separators=(",", ":"),
                    ),
                    str(row["tool_name"]),
                )
                for row in rows
            ]
            conn.executemany(
                """
                UPDATE okf_candidates
                SET status = 'claimed', claimed_at = ?, claim_token = ?,
                    attempt_count = COALESCE(attempt_count, 0) + 1,
                    generator_version = ?, claim_generator_version = ?, related_tools_json = ?,
                    last_attempt_error = NULL
                WHERE tool_name = ? AND COALESCE(status, 'pending') = 'pending'
                """,
                updates,
            )
        claimed = conn.execute(
            f"SELECT {_CANDIDATE_SELECT} FROM okf_candidates "
            f"WHERE claim_token = ? AND tool_name IN ({','.join('?' for _ in names)})"
            if names
            else f"SELECT {_CANDIDATE_SELECT} FROM okf_candidates WHERE 0",
            (token, *names) if names else (),
        ).fetchall()
    by_name = {str(row["tool_name"]): _row_dict(row) for row in claimed}
    return [by_name[name] for name in names if name in by_name]


def recover_stale_claims(
    state_dir: Path,
    *,
    stale_after_seconds: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: str | None = None,
) -> int:
    cutoff = _stale_claim_cutoff(stale_after_seconds=stale_after_seconds, now=now)
    with _connect(state_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _recover_stale_claims_on_connection(
            conn,
            state_dir,
            cutoff=cutoff,
            max_attempts=max_attempts,
        )


def _mark_candidate_done_on_connection(
    conn: sqlite3.Connection,
    *,
    tool_name: str,
    claim_token: str,
    okf_path: Path,
) -> bool:
    cursor = conn.execute(
        """
        UPDATE okf_candidates
        SET status = 'done', okf_path = ?, generator_version = ?, claim_token = NULL, claimed_at = NULL,
            claim_generator_version = NULL, related_tools_json = '[]', last_attempt_error = NULL
        WHERE tool_name = ? AND status = 'claimed' AND claim_token = ? AND claim_generator_version = ?
        """,
        (
            str(okf_path),
            OKF_GENERATOR_VERSION,
            tool_name,
            claim_token,
            OKF_GENERATOR_VERSION,
        ),
    )
    return cursor.rowcount == 1


def mark_candidate_done(state_dir: Path, *, tool_name: str, claim_token: str, okf_path: Path) -> bool:
    with _connect(state_dir) as conn:
        updated = _mark_candidate_done_on_connection(
            conn,
            tool_name=tool_name,
            claim_token=claim_token,
            okf_path=okf_path,
        )
        if updated:
            mark_index_dirty(state_dir)
    return updated


def publish_claimed_okf(
    state_dir: Path,
    *,
    lease_owner: str,
    tool_name: str,
    claim_token: str,
    okf_path: Path,
    publish: Callable[[], None],
    rollback: Callable[[], None],
) -> str:
    """Fence canonical publication against lease takeover and stale-claim recovery."""

    outcome = "stale"
    with _connect(state_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        lease = conn.execute(
            """
            SELECT 1 FROM okf_worker_leases
            WHERE name = ? AND owner = ? AND expires_at > ?
            """,
            (GENERATION_LEASE_NAME, lease_owner, time.time()),
        ).fetchone()
        claim = conn.execute(
            """
            SELECT 1 FROM okf_candidates
            WHERE tool_name = ? AND status = 'claimed' AND claim_token = ?
              AND claim_generator_version = ? AND generator_version = ?
            """,
            (tool_name, claim_token, OKF_GENERATOR_VERSION, OKF_GENERATOR_VERSION),
        ).fetchone()
        if lease is None or claim is None:
            conn.rollback()
            return outcome

        reverted = False

        def revert() -> None:
            nonlocal reverted
            if reverted:
                return
            reverted = True
            try:
                rollback()
            finally:
                conn.rollback()

        try:
            publish()
            validation = validate_okf_file(state_dir, claim_token=claim_token, path=okf_path, _conn=conn)
            if not validation["valid"]:
                revert()
                return "invalid"
            if not _mark_candidate_done_on_connection(
                conn,
                tool_name=tool_name,
                claim_token=claim_token,
                okf_path=okf_path,
            ):
                revert()
                return outcome
            # Publish the dirty marker before committing so process death cannot leave
            # a durable done row whose new artifact is invisible to the next index pass.
            mark_index_dirty(state_dir)
            conn.commit()
            outcome = "done"
        except Exception:
            revert()
            raise
    return outcome


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
            SET status = CASE WHEN COALESCE(attempt_count, 0) >= ? THEN 'error' ELSE 'pending' END,
                claim_token = NULL,
                claimed_at = NULL,
                claim_generator_version = NULL,
                related_tools_json = '[]',
                okf_path = NULL,
                last_attempt_error = ?
            WHERE tool_name = ? AND claim_token = ?
            """,
            (max_attempts, clean_error, tool_name, claim_token),
        )
        return cursor.rowcount == 1


def queue_counts(state_dir: Path) -> dict[str, int]:
    with _connect(state_dir) as conn:
        rows = conn.execute(
            "SELECT COALESCE(status, 'pending') AS status, COUNT(*) AS count "
            "FROM okf_candidates GROUP BY COALESCE(status, 'pending')"
        ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def _allowed_related_tools_from_conn(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    limit: int,
) -> list[str]:
    tool_name = str(row.get("tool_name") or row.get("tool") or "").strip()
    toolset = str(row.get("toolset") or "").strip()
    if not tool_name or not toolset or limit <= 0:
        return []
    rows = conn.execute(
        """
        SELECT tool_name FROM okf_candidates
        WHERE toolset = ? AND tool_name != ?
        ORDER BY COALESCE(use_count, 0) DESC, tool_name ASC
        LIMIT ?
        """,
        (toolset, tool_name, limit),
    ).fetchall()
    return [str(candidate["tool_name"]) for candidate in rows]


def allowed_related_tools(
    state_dir: Path,
    row: Mapping[str, Any],
    *,
    limit: int = DEFAULT_MAX_RELATED_TOOLS,
) -> list[str]:
    """Return a bounded relation allowlist from the candidate's own toolset."""

    with _connect(state_dir) as conn:
        return _allowed_related_tools_from_conn(conn, row, limit=limit)


def _claim_related_tools(row: Mapping[str, Any]) -> list[str]:
    try:
        values = json.loads(str(row.get("related_tools_json") or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    normalized = [str(value).strip() for value in values if isinstance(value, str) and value.strip()]
    return list(dict.fromkeys(normalized))[:DEFAULT_MAX_RELATED_TOOLS]


def candidate_packet(row: Mapping[str, Any], state_dir: Path) -> dict[str, Any]:
    """Return the privacy-safe packet a worker may use to author an OKF."""

    tool_name = str(row.get("tool_name") or "")
    try:
        schema = _stored_schema_projection(row.get("schema_json"))
    except ValueError:
        schema = {}
    try:
        arg_shape = _stored_arg_shape(row.get("arg_shape_json"))
    except ValueError:
        arg_shape = {}
    return {
        "tool": tool_name,
        "tool_name": tool_name,
        "toolset": row.get("toolset"),
        "schema_hash": row.get("schema_hash"),
        "schema": schema,
        "generator_version": row.get("generator_version") or OKF_GENERATOR_VERSION,
        "allowed_related_tools": (
            _claim_related_tools(row) if row.get("status") == "claimed" else allowed_related_tools(state_dir, row)
        ),
        "arg_shape": arg_shape,
        "use_count": int(row.get("use_count") or 0),
        "success_count": int(row.get("success_count") or 0),
        "error_count": int(row.get("error_count") or 0),
        "last_error_type": row.get("last_error_type"),
        "last_error_message": row.get("last_error_message"),
        "claim_token": row.get("claim_token"),
        "target_path": str(okf_file_path(state_dir, tool_name)),
    }


def _claimed_candidate_from_connection(
    conn: sqlite3.Connection,
    *,
    tool_name: str,
    claim_token: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_CANDIDATE_SELECT} FROM okf_candidates "
        "WHERE tool_name = ? AND claim_token = ? AND status = 'claimed'",
        (tool_name, claim_token),
    ).fetchone()
    return _row_dict(row) if row else None


def claimed_candidate(state_dir: Path, *, tool_name: str, claim_token: str) -> dict[str, Any] | None:
    with _connect(state_dir) as conn:
        return _claimed_candidate_from_connection(conn, tool_name=tool_name, claim_token=claim_token)


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


def validate_okf_file(
    state_dir: Path,
    *,
    claim_token: str,
    path: Path,
    _conn: sqlite3.Connection | None = None,
    _content_path: Path | None = None,
) -> dict[str, Any]:
    """Validate a worker-authored OKF before marking a candidate done."""

    errors: list[str] = []
    resolved_state_dir = state_dir.expanduser().resolve()
    allowed_root = okf_dir(resolved_state_dir)
    resolved_path = path.expanduser().resolve()
    resolved_content_path = (_content_path or path).expanduser().resolve()
    if not _path_is_relative_to(resolved_path, allowed_root):
        errors.append(f"path must be under {allowed_root}")
    if not _path_is_relative_to(resolved_content_path, allowed_root):
        errors.append(f"content path must be under {allowed_root}")
    if resolved_path.suffix != ".md":
        errors.append("OKF path must use .md suffix")
    text = _safe_read_text(resolved_content_path, max_chars=80_000)
    if not text:
        errors.append("OKF file is missing or empty")
    if _SECRET_ASSIGNMENT.search(text):
        errors.append("OKF file contains secret-like assignment text")
    frontmatter = _parse_frontmatter(text)
    artifact_type = str(frontmatter.get("artifact_type") or "").strip()
    if artifact_type != "tool_okf":
        errors.append("frontmatter artifact_type must be tool_okf")
    tool_name = str(frontmatter.get("tool") or "").strip()
    if not tool_name:
        errors.append("frontmatter tool is required")
    schema_digest = str(frontmatter.get("schema_hash") or "").strip()
    if not schema_digest:
        errors.append("frontmatter schema_hash is required")
    generator_version = str(frontmatter.get("generator_version") or "").strip()
    if generator_version != OKF_GENERATOR_VERSION:
        errors.append(f"frontmatter generator_version must be {OKF_GENERATOR_VERSION}")
    aliases = _frontmatter_list(frontmatter, "aliases")
    triggers = _frontmatter_list(frontmatter, "triggers")
    when_not_to_use = _frontmatter_list(frontmatter, "when_not_to_use")
    related_tools = _frontmatter_list(frontmatter, "related_tools")
    if not any(_useful_routing_phrase(phrase, tool_name=tool_name) for phrase in [*aliases, *triggers]):
        errors.append("frontmatter aliases or triggers must include at least one specific multi-word routing phrase")
    if any(_GENERIC_NEGATIVE_GUIDANCE.search(phrase) for phrase in when_not_to_use):
        errors.append("frontmatter when_not_to_use must not contain generic privacy or credential guidance")
    if _EPHEMERAL_OKF_TEXT.search(text):
        errors.append("OKF content must not persist runtime counters or transient error fields")

    row = None
    if tool_name:
        row = (
            _claimed_candidate_from_connection(_conn, tool_name=tool_name, claim_token=claim_token)
            if _conn is not None
            else claimed_candidate(resolved_state_dir, tool_name=tool_name, claim_token=claim_token)
        )
    if row is None:
        errors.append("no claimed candidate matches the provided claim token and tool")
    else:
        expected_path = okf_file_path(resolved_state_dir, tool_name).resolve()
        if resolved_path != expected_path:
            errors.append(f"path must match claimed target path {expected_path}")
        if schema_digest != str(row.get("schema_hash") or ""):
            errors.append("frontmatter schema_hash does not match claimed candidate")
        expected_toolset = str(row.get("toolset") or "").strip()
        actual_toolset = str(frontmatter.get("toolset") or "").strip()
        if actual_toolset != expected_toolset:
            errors.append("frontmatter toolset does not match claimed candidate")
        if (
            str(row.get("claim_generator_version") or "") != OKF_GENERATOR_VERSION
            or str(row.get("generator_version") or "") != OKF_GENERATOR_VERSION
        ):
            errors.append("claimed candidate generator version does not match current generator")
        allowed_related = set(_claim_related_tools(row))
        if set(related_tools) - allowed_related:
            errors.append("frontmatter related_tools contains identifiers outside allowed_related_tools")

    return {
        "valid": not errors,
        "errors": errors,
        "tool": tool_name,
        "path": str(resolved_path),
        "claim_token": claim_token,
    }


class _OkfPathCollisionError(RuntimeError):
    pass


OKF_GENERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "okfs": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "schema_hash": {"type": "string"},
                    "title": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "triggers": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "when_not_to_use": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "related_tools": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "body": {"type": "string"},
                },
                "required": [
                    "tool",
                    "schema_hash",
                    "title",
                    "aliases",
                    "triggers",
                    "when_not_to_use",
                    "related_tools",
                    "body",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["okfs"],
    "additionalProperties": False,
}

_GENERATED_ITEM_KEYS = {
    "tool",
    "schema_hash",
    "title",
    "aliases",
    "triggers",
    "when_not_to_use",
    "related_tools",
    "body",
}


def _tool_metadata(tool_name: str) -> tuple[str | None, dict[str, Any] | None]:
    try:
        from tools.registry import registry  # type: ignore

        schema = registry.get_schema(tool_name)
        toolset = registry.get_toolset_for_tool(tool_name)
        return toolset, schema
    except Exception:
        logger.debug("Could not inspect tool registry metadata for %s", tool_name, exc_info=True)
        return None, None


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _inside_okf_worker() -> bool:
    return _truthy_env(OKF_WORKER_ENV)


def _classify_result(result: Any) -> tuple[bool, str | None, str | None]:
    if not isinstance(result, str):
        return True, None, None
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return True, None, None
    if not isinstance(parsed, dict):
        return True, None, None
    if parsed.get("success") is False or bool(parsed.get("error")):
        error = parsed.get("error") or parsed.get("message") or "tool_error"
        return False, "tool_error", str(error)
    return True, None, None


def _classify_hook_outcome(kwargs: Mapping[str, Any]) -> tuple[bool, str | None, str | None]:
    status = kwargs.get("status")
    if isinstance(status, str) and status.strip():
        normalized = status.strip().lower()
        if normalized in {"ok", "success"}:
            return True, None, None
        error_type = kwargs.get("error_type") or normalized
        error_message = kwargs.get("error_message") or kwargs.get("result") or normalized
        return False, str(error_type), str(error_message)
    return _classify_result(kwargs.get("result"))


def _on_post_tool_call(**kwargs: Any) -> None:
    if _inside_okf_worker():
        return
    try:
        cfg = resolve_config()
        if cfg.okf.enabled:
            tool_name = kwargs.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                args = kwargs.get("args")
                if not isinstance(args, dict):
                    args = {}
                success, error_type, error_message = _classify_hook_outcome(kwargs)
                toolset, schema = _tool_metadata(tool_name)
                upsert_tool_candidate(
                    cfg.state_dir,
                    tool_name=tool_name,
                    toolset=toolset,
                    schema=schema,
                    args=args,
                    success=success,
                    error_type=error_type,
                    error_message=error_message,
                )
    except Exception:
        logger.exception("Failed to record local-knowledge OKF tool candidate")
    _on_post_tool_call_implicit_feedback(**kwargs)


def _worker_command(cfg: Config) -> list[str]:
    return [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "local-knowledge",
        "okf-worker",
        "--hermes-home",
        str(cfg.hermes_home),
    ]


def _detached_process_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        creationflags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
        return {"creationflags": creationflags}
    return {"start_new_session": True}


def _start_worker_reaper(process: subprocess.Popen[Any]) -> None:
    """Reap the detached child without making session finalization wait."""

    threading.Thread(
        target=process.wait,
        name=f"local-knowledge-okf-worker-{process.pid}",
        daemon=True,
    ).start()


def _spawn_worker(cfg: Config) -> bool:
    log_path = cfg.state_dir / "okf_worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env[OKF_WORKER_ENV] = "1"
    env["HERMES_HOME"] = str(cfg.hermes_home)
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            _worker_command(cfg),
            cwd=str(cfg.hermes_home),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **_detached_process_kwargs(),
        )
        _start_worker_reaper(process)
        return True
    finally:
        log_handle.close()


def _on_session_finalize(**kwargs: Any) -> bool:
    del kwargs
    if _inside_okf_worker():
        return False
    try:
        cfg = resolve_config()
        if not cfg.okf.enabled or not cfg.okf.auto_generate:
            return False
        lease_seconds = generation_lease_seconds(cfg.okf.max_generation_seconds)
        if not has_generation_work(
            cfg.state_dir,
            min_use_count=cfg.okf.min_use_count,
            stale_after_seconds=lease_seconds,
            timeout_seconds=0.05,
        ):
            return False
        return _spawn_worker(cfg)
    except Exception:
        logger.exception("Failed to launch local-knowledge OKF worker during session finalization")
        return False


def _stored_claim_related_tools(row: Mapping[str, Any]) -> list[str]:
    try:
        values = json.loads(str(row.get("related_tools_json") or "[]"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored related-tools snapshot is not valid JSON") from exc
    if not isinstance(values, list) or len(values) > DEFAULT_MAX_RELATED_TOOLS:
        raise ValueError("stored related-tools snapshot is not a bounded list")
    if not all(isinstance(value, str) and value.strip() and len(value) <= 240 for value in values):
        raise ValueError("stored related-tools snapshot contains an invalid identifier")
    normalized = [value.strip() for value in values]
    if len(set(normalized)) != len(normalized):
        raise ValueError("stored related-tools snapshot contains duplicate identifiers")
    return normalized


def _generation_packet(row: Mapping[str, Any]) -> dict[str, Any]:
    tool_name = str(row.get("tool_name") or "").strip()
    schema_digest = str(row.get("schema_hash") or "").strip()
    if not tool_name or not schema_digest:
        raise ValueError("candidate identity is incomplete")
    return {
        "tool": tool_name,
        "toolset": row.get("toolset"),
        "schema_hash": schema_digest,
        "schema": _stored_schema_projection(row.get("schema_json")),
        "allowed_related_tools": _stored_claim_related_tools(row),
        "arg_shape": _stored_arg_shape(row.get("arg_shape_json")),
    }


def _bounded_list(value: Any, *, limit: int = 8, max_chars: int = 240) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).replace("\x00", "").strip()[:max_chars] for item in value[:limit] if str(item).strip()]


def _quoted(value: Any, *, max_chars: int = 500) -> str:
    clean = str(value).replace("\x00", "").strip()[:max_chars]
    return json.dumps(clean, ensure_ascii=False)


def _render_okf(item: Mapping[str, Any], *, toolset: str | None) -> str:
    tool_name = str(item.get("tool") or "").strip()
    schema_digest = str(item.get("schema_hash") or "").strip()
    title = str(item.get("title") or f"Tool OKF: {tool_name}").strip()[:500]
    body = str(item.get("body") or "").replace("\x00", "").strip()[:4_000]
    lines = [
        "---",
        "artifact_type: tool_okf",
        f"tool: {_quoted(tool_name)}",
    ]
    if toolset:
        lines.append(f"toolset: {_quoted(toolset)}")
    lines.extend(
        [
            f"schema_hash: {_quoted(schema_digest)}",
            f"generator_version: {_quoted(OKF_GENERATOR_VERSION)}",
            f"title: {_quoted(title)}",
            f"generated_at: {_quoted(utc_now())}",
        ]
    )
    for key in ("aliases", "triggers", "when_not_to_use", "related_tools"):
        lines.append(f"{key}:")
        lines.extend(f"  - {_quoted(value, max_chars=240)}" for value in _bounded_list(item.get(key)))
    lines.extend(["---", "", f"# {title}", "", body, ""])
    return "\n".join(lines)


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    descriptor, raw_temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.restore.", suffix=".tmp")
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(previous)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _generated_item_error(
    item: Mapping[str, Any],
    *,
    row: Mapping[str, Any],
) -> str | None:
    if set(item) != _GENERATED_ITEM_KEYS:
        return "generated item has an invalid shape"
    if item.get("tool") != row.get("tool_name") or item.get("schema_hash") != row.get("schema_hash"):
        return "generated identity mismatch"
    for key, max_chars in (("title", 500), ("body", 4_000)):
        value = item.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
            return f"generated {key} is invalid"
    for key in ("aliases", "triggers", "when_not_to_use", "related_tools"):
        values = item.get(key)
        if not (
            isinstance(values, list)
            and len(values) <= 8
            and all(isinstance(value, str) and value.strip() and len(value) <= 240 for value in values)
        ):
            return f"generated {key} is invalid"
    if set(item["related_tools"]) - set(_stored_claim_related_tools(row)):
        return "generated related_tools is outside the claim allowlist"
    return None


def _write_and_complete_item(
    cfg: Config,
    *,
    row: Mapping[str, Any],
    item: Mapping[str, Any],
    lease_owner: str,
) -> bool:
    tool_name = str(row.get("tool_name") or "")
    claim_token = str(row.get("claim_token") or "")
    item_error = _generated_item_error(item, row=row)
    if item_error is not None:
        mark_candidate_error(cfg.state_dir, tool_name=tool_name, claim_token=claim_token, error=item_error)
        return False

    path = okf_file_path(cfg.state_dir, tool_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(_render_okf(item, toolset=str(row.get("toolset") or "").strip() or None))
        temp_path = Path(handle.name)
    previous: bytes | None = None
    published = False

    def publish() -> None:
        nonlocal previous, published
        previous = path.read_bytes() if path.exists() else None
        if previous is not None:
            existing_frontmatter = _parse_frontmatter(previous.decode("utf-8", errors="replace"))
            existing_tool = str(existing_frontmatter.get("tool") or "").strip()
            if existing_tool and existing_tool != tool_name:
                raise _OkfPathCollisionError("OKF target path belongs to a different tool")
        os.replace(temp_path, path)
        published = True

    def rollback() -> None:
        if published:
            _restore_file(path, previous)

    try:
        prevalidation = validate_okf_file(
            cfg.state_dir,
            claim_token=claim_token,
            path=path,
            _content_path=temp_path,
        )
        if not prevalidation["valid"]:
            mark_candidate_error(
                cfg.state_dir,
                tool_name=tool_name,
                claim_token=claim_token,
                error="generated validation failed",
            )
            return False
        try:
            outcome = publish_claimed_okf(
                cfg.state_dir,
                lease_owner=lease_owner,
                tool_name=tool_name,
                claim_token=claim_token,
                okf_path=path,
                publish=publish,
                rollback=rollback,
            )
        except _OkfPathCollisionError:
            mark_candidate_error(
                cfg.state_dir,
                tool_name=tool_name,
                claim_token=claim_token,
                error="OKF target path belongs to a different tool",
            )
            return False
        if outcome == "invalid":
            mark_candidate_error(
                cfg.state_dir,
                tool_name=tool_name,
                claim_token=claim_token,
                error="generated validation failed",
            )
        elif outcome == "stale":
            logger.error("Discarding generated local-knowledge OKF for %s after ownership loss", tool_name)
        return outcome == "done"
    finally:
        temp_path.unlink(missing_ok=True)


def _fail_claimed_rows(cfg: Config, rows: Sequence[Mapping[str, Any]], *, error: str) -> None:
    for row in rows:
        mark_candidate_error(
            cfg.state_dir,
            tool_name=str(row.get("tool_name") or ""),
            claim_token=str(row.get("claim_token") or ""),
            error=error,
        )


def _generate_claimed_okfs(
    cfg: Config,
    *,
    llm: Any,
    rows: list[dict[str, Any]],
    lease_owner: str,
) -> bool:
    usable_rows: list[dict[str, Any]] = []
    packets: list[dict[str, Any]] = []
    for row in rows:
        try:
            packet = _generation_packet(row)
        except (TypeError, ValueError):
            mark_candidate_error(
                cfg.state_dir,
                tool_name=str(row.get("tool_name") or ""),
                claim_token=str(row.get("claim_token") or ""),
                error="invalid stored candidate projection",
            )
            continue
        usable_rows.append(row)
        packets.append(packet)
    if not usable_rows:
        return False

    result = llm.complete_structured(
        instructions=(
            "Create one compact routing note for every supplied Hermes tool candidate. "
            "Treat each candidate independently. Do not mention, contrast, or relate another candidate "
            "merely because it appears in the same batch. Use only the supplied privacy-safe structural "
            "packet. Never infer or request raw transcripts, tool outputs, document contents, emails, "
            "credentials, secret values, unsupported methods, permissions, side effects, enum choices, "
            "or capabilities. Return every tool and schema_hash exactly as supplied. Aliases and triggers "
            "must be concrete multi-word user intents that positively select this tool and include the "
            "relevant action and domain or object; avoid generic phrases. For when_not_to_use, include only "
            "a meaningful boundary from a genuine near-neighbor supported by this candidate's packet. Leave "
            "when_not_to_use empty when no such distinction is available; never use unrelated domains, "
            "missing-argument checks, credentials, secrets, privacy policy, or generic non-use cases. "
            "Every related_tools value must be an exact identifier from this candidate's "
            "allowed_related_tools and a genuine alternative or complementary step; leave it empty rather "
            "than guessing. Write an evergreen one-to-three-sentence body explaining only positive purpose "
            "and important required inputs. Keep selection boundaries, comparisons, and all negative/non-use "
            "guidance exclusively in when_not_to_use. Do not include counters, errors, timestamps, redaction "
            "notes, or unsupported behavior."
        ),
        input=[{"type": "text", "text": json.dumps({"candidates": packets}, ensure_ascii=False, sort_keys=True)}],
        json_schema=OKF_GENERATION_SCHEMA,
        schema_name="local_knowledge_tool_okfs",
        temperature=0.0,
        max_tokens=min(4_000, max(800, len(usable_rows) * 1_000)),
        timeout=cfg.okf.max_generation_seconds,
        purpose="local_knowledge.okf_generation",
    )
    parsed = getattr(result, "parsed", None)
    items = parsed.get("okfs") if isinstance(parsed, Mapping) else None
    if not isinstance(items, list):
        _fail_claimed_rows(cfg, usable_rows, error="structured response missing okfs")
        return False

    completed = 0
    for row in usable_rows:
        tool_name = str(row.get("tool_name") or "")
        matching = [
            item
            for item in items
            if isinstance(item, Mapping) and item.get("tool") == tool_name
        ]
        if len(matching) != 1:
            mark_candidate_error(
                cfg.state_dir,
                tool_name=tool_name,
                claim_token=str(row.get("claim_token") or ""),
                error="structured response omitted or duplicated candidate",
            )
            continue
        completed += int(_write_and_complete_item(cfg, row=row, item=matching[0], lease_owner=lease_owner))
    return completed > 0


def run_worker(*, llm: Any, hermes_home: Path | str | None = None) -> int:
    """Drain one bounded OKF batch through this process's host-owned LLM."""

    cfg = resolve_config(hermes_home)
    if not cfg.okf.enabled or not cfg.okf.auto_generate:
        return 0
    if llm is None:
        logger.error("Local-knowledge OKF worker started without a host-owned ctx.llm facade")
        return 2

    lease_seconds = generation_lease_seconds(cfg.okf.max_generation_seconds)
    owner = uuid.uuid4().hex
    if not acquire_generation_lease(
        cfg.state_dir,
        owner=owner,
        lease_seconds=lease_seconds,
    ):
        return 0

    claimed: list[dict[str, Any]] = []
    try:
        claimed = claim_candidates(
            cfg.state_dir,
            limit=cfg.okf.max_candidates_per_session,
            min_use_count=cfg.okf.min_use_count,
            stale_after_seconds=lease_seconds,
        )
        if not claimed:
            return 0
        return 0 if _generate_claimed_okfs(cfg, llm=llm, rows=claimed, lease_owner=owner) else 1
    except Exception:
        if claimed:
            _fail_claimed_rows(cfg, claimed, error="host LLM generation failed")
        logger.exception("Failed to generate local-knowledge OKFs in detached worker")
        return 1
    finally:
        if not release_generation_lease(cfg.state_dir, owner=owner):
            logger.warning("Local-knowledge OKF worker no longer owned its generation lease at exit")
