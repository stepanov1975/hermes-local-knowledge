"""Learn lightweight routing hints when Hermes consumes search results.

The idea was proposed and initially implemented by @xXLODXx in PR #27:
https://github.com/stepanov1975/hermes-local-knowledge/pull/27
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import resolve_config
from .telemetry import record_implicit_feedback

logger = logging.getLogger(__name__)
MAX_SEARCH_AGE = timedelta(minutes=30)


def _hook_succeeded(kwargs: Mapping[str, Any]) -> bool:
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
    status = kwargs.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip().lower() in {"ok", "success"}
    return True


def _matching_search_event(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    task_id: str,
    artifact_id: str,
) -> tuple[int, str] | None:
    rows = connection.execute(
        """
        SELECT id, ts, query, top_ids_json
        FROM usage_events
        WHERE session_id = ? AND task_id = ?
          AND tool = 'knowledge_search' AND success = 1
        ORDER BY id DESC
        LIMIT 20
        """,
        (session_id, task_id),
    ).fetchall()
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
            returned_ids = json.loads(str(row["top_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if now - timestamp.astimezone(timezone.utc) > MAX_SEARCH_AGE:
            break
        if not isinstance(returned_ids, list) or artifact_id not in map(str, returned_ids):
            continue
        query = str(row["query"] or "").strip()
        return (int(row["id"]), query) if query else None
    return None


def on_post_tool_call(**kwargs: Any) -> None:
    """Record one same-task, recent search-result consumption; never break hooks."""

    try:
        config = resolve_config()
        if not config.implicit_feedback.enabled or kwargs.get("tool_name") != "knowledge_get":
            return
        args = kwargs.get("args")
        if not isinstance(args, dict) or not _hook_succeeded(kwargs):
            return
        artifact_id = str(args.get("artifact_id") or "").strip()
        session_id = str(kwargs.get("session_id") or "").strip()
        task_id = str(kwargs.get("task_id") or "").strip()
        if not artifact_id or not session_id or not task_id:
            return
        usage_db_path = config.state_dir / "usage.sqlite"
        if not usage_db_path.is_file():
            return
        connection = sqlite3.connect(str(usage_db_path), timeout=0.2)
        connection.row_factory = sqlite3.Row
        try:
            found = _matching_search_event(
                connection,
                session_id=session_id,
                task_id=task_id,
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
            usage_db_path=usage_db_path,
        )
    except Exception:
        logger.debug("Failed to record implicit local-knowledge feedback", exc_info=True)
