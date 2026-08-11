"""Implicit usage feedback derived from post_tool_call observations.

When the agent consumes a search result (``knowledge_get`` on an artifact
that a recent ``knowledge_search`` in the same session returned), that
consumption is recorded as an implicit ``useful`` confirmation. Explicit
``knowledge_feedback`` remains authoritative; implicit rows are gated and
ranked below explicit rows by ``routing``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from typing import Any

from .config import resolve_config
from .telemetry import _clean_text, record_implicit_feedback

logger = logging.getLogger(__name__)

IMPLICIT_SOURCE_TOOL = "knowledge_get"
SEARCH_TOOL = "knowledge_search"
RECENT_SEARCH_LOOKBACK = 20


def _hook_succeeded(kwargs: Mapping[str, Any]) -> bool:
    """Return whether the observed tool call succeeded, mirroring OKF."""
    status = kwargs.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip().lower() in {"ok", "success"}
    result = kwargs.get("result")
    if not isinstance(result, str):
        return True
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return True
    if isinstance(parsed, dict) and (
        parsed.get("success") is False or bool(parsed.get("error"))
    ):
        return False
    return True


def _matching_search_event(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    artifact_id: str,
) -> tuple[int, str] | None:
    """Return the newest successful search ``(event_id, query)`` whose
    recorded result page contains ``artifact_id``, or ``None``."""

    rows = connection.execute(
        """
        SELECT id, query, top_ids_json
        FROM usage_events
        WHERE session_id = ? AND tool = ? AND success = 1
        ORDER BY id DESC
        LIMIT ?
        """,
        (session_id, SEARCH_TOOL, RECENT_SEARCH_LOOKBACK),
    ).fetchall()
    for row in rows:
        try:
            returned_ids = json.loads(str(row["top_ids_json"] or "[]"))
        except json.JSONDecodeError:
            continue
        if not isinstance(returned_ids, list):
            continue
        if artifact_id in {str(value) for value in returned_ids}:
            return int(row["id"]), str(row["query"] or "").strip()
    return None


def on_post_tool_call(**kwargs: Any) -> None:
    """Record one implicit confirmation when a search result is consumed.

    Invoked from the plugin's single ``post_tool_call`` hook. Fails open
    silently: a misconfigured or unavailable database never raises here.
    """

    try:
        cfg = resolve_config()
        if not cfg.implicit_feedback.enabled:
            return
        if kwargs.get("tool_name") != IMPLICIT_SOURCE_TOOL:
            return
        args = kwargs.get("args")
        if not isinstance(args, dict):
            return
        artifact_id = str(args.get("artifact_id") or "").strip()
        if not artifact_id:
            return
        if not _hook_succeeded(kwargs):
            return
        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return

        usage_db_path = cfg.state_dir / "usage.sqlite"
        if not usage_db_path.is_file():
            return
        connection = sqlite3.connect(
            f"file:{usage_db_path}?mode=ro",
            uri=True,
            timeout=0.5,
        )
        connection.row_factory = sqlite3.Row
        try:
            found = _matching_search_event(
                connection,
                session_id=session_id,
                artifact_id=artifact_id,
            )
        finally:
            connection.close()
        if found is None:
            return
        search_event_id, query = found
        if not query:
            return
        record_implicit_feedback(
            cfg.source_root,
            search_event_id=search_event_id,
            query=query,
            artifact_id=artifact_id,
            context={
                "session_id": _clean_text(kwargs.get("session_id"), limit=128),
                "task_id": _clean_text(kwargs.get("task_id"), limit=128),
                "tool_call_id": _clean_text(kwargs.get("tool_call_id"), limit=128),
            },
            usage_db_path=usage_db_path,
        )
    except Exception:
        logger.debug("Failed to record implicit local-knowledge feedback", exc_info=True)
