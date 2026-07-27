"""Hermes tool handlers for local knowledge tools."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import index
from .runtime import _coerce_bool, _coerce_int, _runtime_config
from .schemas import FEEDBACK_RATINGS
from .service import LocalKnowledgeService
from .telemetry import _record_usage, _usage_context
from .tooling import tool_error, tool_result


def _default_service_factory() -> LocalKnowledgeService:
    return LocalKnowledgeService(_runtime_config())


def _validate_args(args: Any) -> str | None:
    if isinstance(args, dict):
        return None
    return tool_error("args must be an object", success=False)


def _exception_fields(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, index.NewerIndexFormatError):
        return {
            "error_code": "newer_index_format",
            "expected_index_format_version": exc.expected_version,
            "actual_index_format_version": exc.actual_version,
        }
    return {}


def _record_handler_usage(
    service: LocalKnowledgeService | None,
    **kwargs: Any,
) -> int | None:
    if service is not None:
        return service.record_usage(**kwargs)
    return _record_usage(None, **kwargs)


def _handle_search(
    args: dict[str, Any],
    *,
    service_factory: Callable[[], LocalKnowledgeService] = _default_service_factory,
    **kwargs: Any,
) -> str:
    if error := _validate_args(args):
        return error
    started = time.perf_counter()
    context = _usage_context(kwargs)
    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query is required", success=False)

    limit = _coerce_int(args.get("limit"), default=8, minimum=1, maximum=30)
    artifact_type = str(args.get("artifact_type") or "").strip()
    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    service: LocalKnowledgeService | None = None
    db_path: Path | None = None
    meta: dict[str, Any] = {}

    try:
        service = service_factory()
        db_path = service.db_path
        rows, meta = service.search(
            query,
            limit=limit,
            artifact_type=artifact_type or None,
            rebuild=rebuild,
        )
        rows = rows[:limit]
        event_id = service.record_usage(
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
            index_metadata=meta,
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
        event_id = _record_handler_usage(
            service,
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
            index_metadata=meta,
        )
        return tool_error(
            message,
            success=False,
            usage_event_id=event_id,
            **_exception_fields(exc),
        )


def _handle_get(
    args: dict[str, Any],
    *,
    service_factory: Callable[[], LocalKnowledgeService] = _default_service_factory,
    **kwargs: Any,
) -> str:
    if error := _validate_args(args):
        return error
    started = time.perf_counter()
    context = _usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return tool_error("artifact_id is required", success=False)

    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    include_neighbors = _coerce_bool(args.get("include_neighbors"), default=False)
    service: LocalKnowledgeService | None = None
    db_path: Path | None = None
    meta: dict[str, Any] = {}

    try:
        service = service_factory()
        db_path = service.db_path
        artifact, meta = service.get(artifact_id, rebuild=rebuild)
        if artifact is None:
            message = f"Artifact not found: {artifact_id}"
            event_id = service.record_usage(
                tool="knowledge_get",
                success=False,
                artifact_id=artifact_id,
                rebuild_requested=rebuild,
                rebuilt=bool(meta.get("rebuilt")),
                error=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                context=context,
                index_metadata=meta,
            )
            return tool_error(
                message,
                success=False,
                artifact_id=artifact_id,
                usage_event_id=event_id,
                **meta,
            )
        neighbors = (
            service.neighbors(artifact_id, ensure=False)[0]
            if include_neighbors
            else None
        )
        event_id = service.record_usage(
            tool="knowledge_get",
            success=True,
            artifact_id=artifact_id,
            rebuild_requested=rebuild,
            rebuilt=bool(meta.get("rebuilt")),
            result_count=1,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
            index_metadata=meta,
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
        event_id = _record_handler_usage(
            service,
            tool="knowledge_get",
            success=False,
            artifact_id=artifact_id,
            rebuild_requested=rebuild,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
            index_metadata=meta,
        )
        return tool_error(
            message,
            success=False,
            usage_event_id=event_id,
            **_exception_fields(exc),
        )


def _handle_neighbors(
    args: dict[str, Any],
    *,
    service_factory: Callable[[], LocalKnowledgeService] = _default_service_factory,
    **kwargs: Any,
) -> str:
    if error := _validate_args(args):
        return error
    started = time.perf_counter()
    context = _usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return tool_error("artifact_id is required", success=False)

    limit = _coerce_int(args.get("limit"), default=20, minimum=1, maximum=50)
    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    service: LocalKnowledgeService | None = None
    db_path: Path | None = None
    meta: dict[str, Any] = {}

    try:
        service = service_factory()
        db_path = service.db_path
        rows, meta = service.neighbors(artifact_id, rebuild=rebuild)
        artifact, _artifact_meta = service.get(artifact_id, ensure=False)
        if artifact is None:
            message = f"Artifact not found: {artifact_id}"
            event_id = service.record_usage(
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
                index_metadata=meta,
            )
            return tool_error(
                message,
                success=False,
                artifact_id=artifact_id,
                usage_event_id=event_id,
                **meta,
            )
        rows = rows[:limit]
        event_id = service.record_usage(
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
            index_metadata=meta,
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
        event_id = _record_handler_usage(
            service,
            tool="knowledge_neighbors",
            success=False,
            artifact_id=artifact_id,
            limit_value=limit,
            rebuild_requested=rebuild,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            context=context,
            index_metadata=meta,
        )
        return tool_error(
            message,
            success=False,
            usage_event_id=event_id,
            **_exception_fields(exc),
        )


def _handle_feedback(
    args: dict[str, Any],
    *,
    service_factory: Callable[[], LocalKnowledgeService] = _default_service_factory,
    **kwargs: Any,
) -> str:
    if error := _validate_args(args):
        return error
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
    service: LocalKnowledgeService | None = None
    try:
        service = service_factory()
        feedback_id = service.feedback(
            rating=rating,
            event_id=event_id,
            query=query,
            artifact_id=artifact_id,
            note=note,
            context=context,
        )
        usage_event_id = service.record_usage(
            tool="knowledge_feedback",
            success=True,
            query=query,
            artifact_id=artifact_id,
            result_count=1,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=service.usage_db_path,
            context=context,
        )
        return tool_result(
            {
                "success": True,
                "feedback_id": feedback_id,
                "usage_event_id": usage_event_id,
                "rating": rating,
                "event_id": event_id,
                "usage_db_path": str(service.usage_db_path),
            }
        )
    except Exception as exc:
        message = f"knowledge_feedback failed: {type(exc).__name__}: {exc}"
        usage_event_id = _record_handler_usage(
            service,
            tool="knowledge_feedback",
            success=False,
            query=query,
            artifact_id=artifact_id,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=usage_event_id)


def _handle_usage_report(
    args: dict[str, Any],
    *,
    service_factory: Callable[[], LocalKnowledgeService] = _default_service_factory,
    **kwargs: Any,
) -> str:
    if error := _validate_args(args):
        return error
    started = time.perf_counter()
    context = _usage_context(kwargs)
    days = _coerce_int(args.get("days"), default=14, minimum=1, maximum=365)
    limit = _coerce_int(args.get("limit"), default=10, minimum=1, maximum=50)
    service: LocalKnowledgeService | None = None
    try:
        service = service_factory()
        report = service.usage_report(days=days, limit=limit)
        usage_event_id = service.record_usage(
            tool="knowledge_usage_report",
            success=True,
            limit_value=limit,
            result_count=int(report.get("total_events") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=service.usage_db_path,
            context=context,
        )
        report["usage_event_id"] = usage_event_id
        return tool_result(report)
    except Exception as exc:
        message = f"knowledge_usage_report failed: {type(exc).__name__}: {exc}"
        usage_event_id = _record_handler_usage(
            service,
            tool="knowledge_usage_report",
            success=False,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            context=context,
        )
        return tool_error(message, success=False, usage_event_id=usage_event_id)
