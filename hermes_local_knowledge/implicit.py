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
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import resolve_config
from .telemetry import attach_usage_event_turn_id, record_implicit_feedback

logger = logging.getLogger(__name__)
MAX_SEARCH_AGE = timedelta(minutes=30)
_TURN_CONTEXT: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "local_knowledge_implicit_turn",
    default=None,
)


def on_pre_llm_call(**kwargs: Any) -> None:
    """Bind host turn identity for deferred tools whose bridge drops it."""

    session_id = str(kwargs.get("session_id") or "").strip()
    task_id = str(kwargs.get("task_id") or "").strip()
    turn_id = str(kwargs.get("turn_id") or "").strip()
    _TURN_CONTEXT.set(
        (session_id, task_id, turn_id)
        if session_id and task_id and turn_id
        else None
    )


def on_session_end(**_kwargs: Any) -> None:
    """Discard the completed turn's bridge fallback identity."""

    _TURN_CONTEXT.set(None)


def _resolved_turn_id(*, session_id: str, task_id: str, turn_id: Any) -> str:
    direct = str(turn_id or "").strip()
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
        try:
            timestamp = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
            baseline_ids = json.loads(str(row["baseline_top_ids_json"] or "[]"))
            final_ids = json.loads(str(row["top_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if now - timestamp.astimezone(timezone.utc) > MAX_SEARCH_AGE:
            break
        if (
            not isinstance(baseline_ids, list)
            or not all(isinstance(item, str) for item in baseline_ids)
            or artifact_id not in baseline_ids
            or not isinstance(final_ids, list)
            or not all(isinstance(item, str) for item in final_ids)
            or artifact_id not in final_ids
        ):
            continue
        query = str(row["query"] or "").strip()
        return (int(row["id"]), query) if query else None
    return None


def on_post_tool_call(**kwargs: Any) -> None:
    """Record one same-turn, recent, caller-visible baseline-result consumption; never break hooks."""

    try:
        config = resolve_config()
        tool_name = kwargs.get("tool_name")
        if not config.implicit_feedback.enabled or tool_name not in {
            "knowledge_search",
            "knowledge_get",
        }:
            return
        if not _hook_succeeded(kwargs):
            return
        session_id = str(kwargs.get("session_id") or "").strip()
        task_id = str(kwargs.get("task_id") or "").strip()
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
                    usage_db_path=usage_db_path,
                )
            return
        args = kwargs.get("args")
        if not isinstance(args, dict):
            return
        artifact_id = str(args.get("artifact_id") or "").strip()
        if not artifact_id:
            return
        connection = sqlite3.connect(str(usage_db_path), timeout=0.2)
        connection.row_factory = sqlite3.Row
        try:
            found = _matching_search_event(
                connection,
                session_id=session_id,
                task_id=task_id,
                turn_id=turn_id,
                root=str(config.source_root),
                artifact_id=artifact_id,
            )
        finally:
            connection.close()
        if found is None:
            return
        event_id, query = found
        record_implicit_feedback(
            config.source_root,
            search_event_id=event_id,
            query=query,
            artifact_id=artifact_id,
            session_id=session_id,
            task_id=task_id,
            turn_id=turn_id,
            usage_db_path=usage_db_path,
        )
    except Exception:
        logger.debug("Failed to record implicit local-knowledge feedback", exc_info=True)
