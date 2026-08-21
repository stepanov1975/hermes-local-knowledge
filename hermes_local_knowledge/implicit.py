"""Learn lightweight routing hints when Hermes consumes search results.

The idea was proposed and initially implemented by @xXLODXx in PR #27:
https://github.com/stepanov1975/hermes-local-knowledge/pull/27
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import resolve_config
from .index import connect_readonly
from .routing import IMPLICIT_FEEDBACK_MAX_SEARCH_AGE, _parsed_utc_timestamp
from .telemetry import (
    _clean_text,
    attach_usage_event_turn_id,
    record_implicit_feedback,
)

logger = logging.getLogger(__name__)
_IMPLICIT_CONSUMER_TOOLS = {"knowledge_get", "skill_view", "read_file"}
_FILE_CONSUMER_TYPES = {
    "skill_view": {"skill", "skill_support_doc"},
    "read_file": {
        "doc",
        "mcp_server",
        "memory_doc",
        "runbook",
        "script",
        "skill",
        "skill_support_doc",
        "tool_okf",
    },
}
_TURN_CONTEXT: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "local_knowledge_implicit_turn",
    default=None,
)


def _clean_identity(value: Any) -> str:
    return _clean_text(value, limit=128)


def on_pre_llm_call(**kwargs: Any) -> None:
    """Bind host turn identity for deferred tools whose bridge drops it."""

    session_id = _clean_identity(kwargs.get("session_id"))
    task_id = _clean_identity(kwargs.get("task_id"))
    turn_id = _clean_identity(kwargs.get("turn_id"))
    _TURN_CONTEXT.set(
        (session_id, task_id, turn_id)
        if session_id and task_id and turn_id
        else None
    )


def on_session_end(**_kwargs: Any) -> None:
    """Discard the completed turn's bridge fallback identity."""

    _TURN_CONTEXT.set(None)


def _resolved_turn_id(*, session_id: str, task_id: str, turn_id: Any) -> str:
    direct = _clean_identity(turn_id)
    if direct:
        return direct
    current = _TURN_CONTEXT.get()
    if current is None or current[:2] != (session_id, task_id):
        return ""
    return current[2]


def _hook_succeeded(kwargs: Mapping[str, Any]) -> bool:
    status = kwargs.get("status")
    if (
        isinstance(status, str)
        and status.strip()
        and status.strip().lower() not in {"ok", "success"}
    ):
        return False
    result = kwargs.get("result")
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if payload.get("success") is False or bool(payload.get("error")):
                return False
            if payload.get("success") is True:
                return True
    return True


def _usage_event_id(result: Any) -> int | None:
    if not isinstance(result, str):
        return None
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("usage_event_id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _result_payload(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, str):
        return None
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _file_consumer_path(tool_name: str, args: Mapping[str, Any], result: Any) -> str:
    payload = _result_payload(result)
    if payload is None:
        return ""
    if tool_name == "skill_view":
        source_path = payload.get("_source_path")
        return str(source_path).strip() if payload.get("success") is True and source_path else ""
    if tool_name == "read_file":
        path = args.get("path")
        return str(path).strip() if isinstance(payload.get("content"), str) and path else ""
    return ""


def _resolved_path(path: str, *, root: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def _artifact_path(path: str, artifact_type: str, *, root: Path) -> Path:
    candidate = _resolved_path(path, root=root)
    return candidate / "SKILL.md" if artifact_type == "skill" else candidate


def _matching_file_artifact(
    index_connection: sqlite3.Connection,
    *,
    artifact_ids: list[str],
    tool_name: str,
    consumer_path: Path,
    root: Path,
) -> str | None:
    if not artifact_ids:
        return None
    placeholders = ",".join("?" for _ in artifact_ids)
    rows = index_connection.execute(
        f"SELECT id, type, path FROM artifacts WHERE id IN ({placeholders})",
        artifact_ids,
    ).fetchall()
    allowed_types = _FILE_CONSUMER_TYPES[tool_name]
    matches = [
        str(row["id"])
        for row in rows
        if str(row["type"]) in allowed_types
        and _artifact_path(str(row["path"]), str(row["type"]), root=root) == consumer_path
    ]
    return matches[0] if len(matches) == 1 else None


def _matching_file_search_event(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    task_id: str,
    turn_id: str,
    root: Path,
    index_db_path: Path,
    tool_name: str,
    consumer_path: str,
    consumer_api_request_id: str,
) -> tuple[int, str, str] | None:
    if not index_db_path.is_file():
        return None
    rows = connection.execute(
        """
        SELECT id, ts, query, baseline_top_ids_json, top_ids_json,
               index_jsonl_sha256, api_request_id
        FROM usage_events
        WHERE session_id = ? AND task_id = ? AND turn_id = ? AND root = ?
          AND tool = 'knowledge_search' AND success = 1
        ORDER BY id DESC
        LIMIT 20
        """,
        (session_id, task_id, turn_id, str(root)),
    ).fetchall()
    resolved_consumer_path = _resolved_path(consumer_path, root=root)
    now = datetime.now(timezone.utc)
    index_connection = connect_readonly(index_db_path)
    try:
        index_hash_row = index_connection.execute(
            "SELECT value FROM metadata WHERE key = 'jsonl_sha256'"
        ).fetchone()
        current_index_hash = str(index_hash_row[0]) if index_hash_row is not None else ""
        if not current_index_hash:
            return None
        for row in rows:
            timestamp = _parsed_utc_timestamp(row["ts"])
            try:
                baseline_ids = json.loads(str(row["baseline_top_ids_json"] or "[]"))
                final_ids = json.loads(str(row["top_ids_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if timestamp is None or timestamp > now or now - timestamp > IMPLICIT_FEEDBACK_MAX_SEARCH_AGE:
                continue
            search_api_request_id = str(row["api_request_id"] or "")
            if not search_api_request_id or search_api_request_id == consumer_api_request_id:
                continue
            if (
                str(row["index_jsonl_sha256"] or "") != current_index_hash
                or not isinstance(baseline_ids, list)
                or not all(isinstance(item, str) for item in baseline_ids)
                or not isinstance(final_ids, list)
                or not all(isinstance(item, str) for item in final_ids)
            ):
                continue
            artifact_id = _matching_file_artifact(
                index_connection,
                artifact_ids=final_ids,
                tool_name=tool_name,
                consumer_path=resolved_consumer_path,
                root=root,
            )
            if artifact_id is None:
                continue
            if artifact_id not in baseline_ids:
                return None
            query = str(row["query"] or "").strip()
            if query:
                return int(row["id"]), query, artifact_id
    finally:
        index_connection.close()
    return None


def _matching_search_event(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    task_id: str,
    turn_id: str,
    root: str,
    artifact_id: str,
) -> tuple[int, str] | None:
    rows = connection.execute(
        """
        SELECT id, ts, query, baseline_top_ids_json, top_ids_json
        FROM usage_events
        WHERE session_id = ? AND task_id = ? AND turn_id = ? AND root = ?
          AND tool = 'knowledge_search' AND success = 1
        ORDER BY id DESC
        LIMIT 20
        """,
        (session_id, task_id, turn_id, root),
    ).fetchall()
    now = datetime.now(timezone.utc)
    for row in rows:
        timestamp = _parsed_utc_timestamp(row["ts"])
        try:
            baseline_ids = json.loads(str(row["baseline_top_ids_json"] or "[]"))
            final_ids = json.loads(str(row["top_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if timestamp is None:
            continue
        if timestamp > now:
            continue
        if now - timestamp > IMPLICIT_FEEDBACK_MAX_SEARCH_AGE:
            continue
        if (
            not isinstance(baseline_ids, list)
            or not all(isinstance(item, str) for item in baseline_ids)
            or not isinstance(final_ids, list)
            or not all(isinstance(item, str) for item in final_ids)
        ):
            continue
        if artifact_id not in final_ids:
            continue
        if artifact_id not in baseline_ids:
            return None
        query = str(row["query"] or "").strip()
        if not query:
            continue
        return int(row["id"]), query
    return None


def on_post_tool_call(**kwargs: Any) -> None:
    """Record one same-turn, recent, caller-visible baseline-result consumption; never break hooks."""

    try:
        tool_name = kwargs.get("tool_name")
        if tool_name != "knowledge_search" and tool_name not in _IMPLICIT_CONSUMER_TOOLS:
            return
        config = resolve_config()
        if not config.implicit_feedback.enabled:
            return
        if not _hook_succeeded(kwargs):
            return
        session_id = _clean_identity(kwargs.get("session_id"))
        task_id = _clean_identity(kwargs.get("task_id"))
        api_request_id = _clean_identity(kwargs.get("api_request_id"))
        turn_id = _resolved_turn_id(
            session_id=session_id,
            task_id=task_id,
            turn_id=kwargs.get("turn_id"),
        )
        if not session_id or not task_id or not turn_id:
            return
        usage_db_path = config.state_dir / "usage.sqlite"
        if not usage_db_path.is_file():
            return
        if tool_name == "knowledge_search":
            event_id = _usage_event_id(kwargs.get("result"))
            if event_id is not None:
                attach_usage_event_turn_id(
                    config.source_root,
                    event_id=event_id,
                    session_id=session_id,
                    task_id=task_id,
                    turn_id=turn_id,
                    api_request_id=api_request_id,
                    usage_db_path=usage_db_path,
                )
            return
        args = kwargs.get("args")
        if not isinstance(args, dict):
            return
        connection = sqlite3.connect(str(usage_db_path), timeout=0.2)
        connection.row_factory = sqlite3.Row
        try:
            if tool_name == "knowledge_get":
                artifact_id = str(args.get("artifact_id") or "").strip()
                if not artifact_id:
                    return
                direct_match = _matching_search_event(
                    connection,
                    session_id=session_id,
                    task_id=task_id,
                    turn_id=turn_id,
                    root=str(config.source_root),
                    artifact_id=artifact_id,
                )
                found = (
                    (*direct_match, artifact_id)
                    if direct_match is not None
                    else None
                )
            else:
                if not api_request_id:
                    return
                consumer_path = _file_consumer_path(tool_name, args, kwargs.get("result"))
                if not consumer_path:
                    return
                found = _matching_file_search_event(
                    connection,
                    session_id=session_id,
                    task_id=task_id,
                    turn_id=turn_id,
                    root=config.source_root,
                    index_db_path=config.state_dir / "index.sqlite",
                    tool_name=tool_name,
                    consumer_path=consumer_path,
                    consumer_api_request_id=api_request_id,
                )
        finally:
            connection.close()
        if found is None:
            return
        event_id, query, artifact_id = found
        record_implicit_feedback(
            config.source_root,
            search_event_id=event_id,
            query=query,
            artifact_id=artifact_id,
            session_id=session_id,
            task_id=task_id,
            turn_id=turn_id,
            usage_db_path=usage_db_path,
            consumer_tool=str(tool_name),
        )
    except Exception:
        logger.debug("Failed to record implicit local-knowledge feedback", exc_info=True)
