"""Hermes tool handlers for local knowledge tools."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .runtime import _coerce_bool, _coerce_int, _ensure_index, _repo_root, _usage_db_path
from .schemas import FEEDBACK_RATINGS
from .search import search_index
from .storage import get_artifact, get_neighbors
from .telemetry import _record_feedback, _record_usage, _usage_context, _usage_report
from .tooling import tool_error, tool_result


def _handle_search(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query is required", success=False)

    limit = _coerce_int(args.get("limit"), default=8, minimum=1, maximum=30)
    artifact_type = str(args.get("artifact_type") or "").strip()
    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    root: Path | None = None
    db_path: Path | None = None

    try:
        root = _repo_root()
        db_path, meta = _ensure_index(root, rebuild=rebuild)
        fetch_limit = limit * 3 if artifact_type else limit
        rows = search_index(db_path, query, limit=fetch_limit)
        if artifact_type:
            rows = [row for row in rows if row.get("type") == artifact_type]
        rows = rows[:limit]
        event_id = _record_usage(
            root,
            tool="knowledge_search",
            success=True,
            query=query,
            artifact_type=artifact_type,
            limit_value=limit,
            rebuild_requested=rebuild,
            rebuilt=bool(meta.get("rebuilt")),
            result_count=len(rows),
            top_ids=[str(row.get("id")) for row in rows[:5]],
            top_types=[str(row.get("type")) for row in rows[:5]],
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_result(
            {
                "success": True,
                "query": query,
                "artifact_type": artifact_type or None,
                "limit": limit,
                "results": rows,
                "usage_event_id": event_id,
                **meta,
            }
        )
    except Exception as exc:
        message = f"knowledge_search failed: {type(exc).__name__}: {exc}"
        event_id = _record_usage(
            root,
            tool="knowledge_search",
            success=False,
            query=query,
            artifact_type=artifact_type,
            limit_value=limit,
            rebuild_requested=rebuild,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=event_id)

def _handle_get(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return tool_error("artifact_id is required", success=False)

    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    include_neighbors = _coerce_bool(args.get("include_neighbors"), default=False)
    root: Path | None = None
    db_path: Path | None = None

    try:
        root = _repo_root()
        db_path, meta = _ensure_index(root, rebuild=rebuild)
        artifact = get_artifact(db_path, artifact_id)
        if artifact is None:
            message = f"Artifact not found: {artifact_id}"
            event_id = _record_usage(
                root,
                tool="knowledge_get",
                success=False,
                artifact_id=artifact_id,
                rebuild_requested=rebuild,
                rebuilt=bool(meta.get("rebuilt")),
                error=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                context=context,
            )
            return tool_error(
                message,
                success=False,
                artifact_id=artifact_id,
                usage_event_id=event_id,
                **meta,
            )
        neighbors = get_neighbors(db_path, artifact_id) if include_neighbors else None
        event_id = _record_usage(
            root,
            tool="knowledge_get",
            success=True,
            artifact_id=artifact_id,
            rebuild_requested=rebuild,
            rebuilt=bool(meta.get("rebuilt")),
            result_count=1,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        payload: dict[str, Any] = {
            "success": True,
            "artifact": artifact,
            "usage_event_id": event_id,
            **meta,
        }
        if neighbors is not None:
            payload["neighbors"] = neighbors
        return tool_result(payload)
    except Exception as exc:
        message = f"knowledge_get failed: {type(exc).__name__}: {exc}"
        event_id = _record_usage(
            root,
            tool="knowledge_get",
            success=False,
            artifact_id=artifact_id,
            rebuild_requested=rebuild,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=event_id)

def _handle_neighbors(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return tool_error("artifact_id is required", success=False)

    limit = _coerce_int(args.get("limit"), default=20, minimum=1, maximum=50)
    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    root: Path | None = None
    db_path: Path | None = None

    try:
        root = _repo_root()
        db_path, meta = _ensure_index(root, rebuild=rebuild)
        artifact = get_artifact(db_path, artifact_id)
        if artifact is None:
            message = f"Artifact not found: {artifact_id}"
            event_id = _record_usage(
                root,
                tool="knowledge_neighbors",
                success=False,
                artifact_id=artifact_id,
                limit_value=limit,
                rebuild_requested=rebuild,
                rebuilt=bool(meta.get("rebuilt")),
                error=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                context=context,
            )
            return tool_error(
                message,
                success=False,
                artifact_id=artifact_id,
                usage_event_id=event_id,
                **meta,
            )
        rows = get_neighbors(db_path, artifact_id)[:limit]
        event_id = _record_usage(
            root,
            tool="knowledge_neighbors",
            success=True,
            artifact_id=artifact_id,
            limit_value=limit,
            rebuild_requested=rebuild,
            rebuilt=bool(meta.get("rebuilt")),
            result_count=len(rows),
            top_ids=[str(row.get("id")) for row in rows[:5]],
            top_types=[str(row.get("type")) for row in rows[:5]],
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_result(
            {
                "success": True,
                "artifact_id": artifact_id,
                "neighbors": rows,
                "limit": limit,
                "usage_event_id": event_id,
                **meta,
            }
        )
    except Exception as exc:
        message = f"knowledge_neighbors failed: {type(exc).__name__}: {exc}"
        event_id = _record_usage(
            root,
            tool="knowledge_neighbors",
            success=False,
            artifact_id=artifact_id,
            limit_value=limit,
            rebuild_requested=rebuild,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=event_id)

def _handle_feedback(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    rating = str(args.get("rating") or "").strip().lower()
    if rating not in FEEDBACK_RATINGS:
        return tool_error(
            f"rating must be one of: {', '.join(sorted(FEEDBACK_RATINGS))}",
            success=False,
        )
    event_id_raw = args.get("event_id")
    try:
        event_id = int(event_id_raw) if event_id_raw is not None else None
    except Exception:
        return tool_error("event_id must be an integer when provided", success=False)

    query = str(args.get("query") or "")
    artifact_id = str(args.get("artifact_id") or "")
    note = str(args.get("note") or "")
    root: Path | None = None
    try:
        root = _repo_root()
        feedback_id = _record_feedback(
            root,
            rating=rating,
            event_id=event_id,
            query=query,
            artifact_id=artifact_id,
            note=note,
            context=context,
        )
        usage_event_id = _record_usage(
            root,
            tool="knowledge_feedback",
            success=True,
            query=query,
            artifact_id=artifact_id,
            result_count=1,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=_usage_db_path(root),
            context=context,
        )
        return tool_result(
            {
                "success": True,
                "feedback_id": feedback_id,
                "usage_event_id": usage_event_id,
                "rating": rating,
                "event_id": event_id,
                "usage_db_path": str(_usage_db_path(root)),
            }
        )
    except Exception as exc:
        message = f"knowledge_feedback failed: {type(exc).__name__}: {exc}"
        usage_event_id = _record_usage(
            root,
            tool="knowledge_feedback",
            success=False,
            query=query,
            artifact_id=artifact_id,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=usage_event_id)

def _handle_usage_report(args: dict[str, Any], **kwargs) -> str:
    started = time.perf_counter()
    context = _usage_context(kwargs)
    days = _coerce_int(args.get("days"), default=14, minimum=1, maximum=365)
    limit = _coerce_int(args.get("limit"), default=10, minimum=1, maximum=50)
    root: Path | None = None
    try:
        root = _repo_root()
        report = _usage_report(root, days=days, limit=limit)
        usage_event_id = _record_usage(
            root,
            tool="knowledge_usage_report",
            success=True,
            limit_value=limit,
            result_count=int(report.get("total_events") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=_usage_db_path(root),
            context=context,
        )
        report["usage_event_id"] = usage_event_id
        return tool_result(report)
    except Exception as exc:
        message = f"knowledge_usage_report failed: {type(exc).__name__}: {exc}"
        usage_event_id = _record_usage(
            root,
            tool="knowledge_usage_report",
            success=False,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=usage_event_id)
