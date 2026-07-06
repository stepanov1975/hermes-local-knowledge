"""Hermes tool handlers for local knowledge tools."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .runtime import _coerce_bool, _coerce_int, _ensure_index, _repo_root, _usage_db_path
from .schemas import FEEDBACK_RATINGS
from .search import search_index
from .storage import get_artifact, get_neighbors
from .telemetry import _record_feedback, _record_usage, _usage_context, _usage_report
from .tooling import tool_error, tool_result


@dataclass(frozen=True)
class HandlerDeps:
    """Dependency seams for plugin compatibility wrappers and tests."""

    coerce_bool: Callable[..., bool] = _coerce_bool
    coerce_int: Callable[..., int] = _coerce_int
    ensure_index: Callable[..., tuple[Path, dict[str, Any]]] = _ensure_index
    get_artifact: Callable[..., dict[str, Any] | None] = get_artifact
    get_neighbors: Callable[..., list[dict[str, Any]]] = get_neighbors
    index_module: Callable[[Path], Any] | None = None
    record_feedback: Callable[..., int | None] = _record_feedback
    record_usage: Callable[..., int | None] = _record_usage
    repo_root: Callable[[], Path] = _repo_root
    search_index: Callable[..., list[dict[str, Any]]] = search_index
    tool_error: Callable[..., str] = tool_error
    tool_result: Callable[..., str] = tool_result
    usage_context: Callable[..., dict[str, Any]] = _usage_context
    usage_db_path: Callable[[Path], Path] = _usage_db_path
    usage_report: Callable[..., dict[str, Any]] = _usage_report


def _handler_deps(deps: HandlerDeps | None) -> HandlerDeps:
    return deps if deps is not None else HandlerDeps()


def _index_attr(deps: HandlerDeps, root: Path, name: str, fallback):
    if deps.index_module is None:
        return fallback
    return getattr(deps.index_module(root), name, fallback)

def _validate_args(args: Any, deps: HandlerDeps) -> str | None:
    if isinstance(args, dict):
        return None
    return deps.tool_error("args must be an object", success=False)


def _handle_search(args: dict[str, Any], *, deps: HandlerDeps | None = None, **kwargs) -> str:
    deps = _handler_deps(deps)
    if error := _validate_args(args, deps):
        return error
    started = time.perf_counter()
    context = deps.usage_context(kwargs)
    query = str(args.get("query") or "").strip()
    if not query:
        return deps.tool_error("query is required", success=False)

    limit = deps.coerce_int(args.get("limit"), default=8, minimum=1, maximum=30)
    artifact_type = str(args.get("artifact_type") or "").strip()
    rebuild = deps.coerce_bool(args.get("rebuild"), default=False)
    root: Path | None = None
    db_path: Path | None = None
    meta: dict[str, Any] = {}

    try:
        root = deps.repo_root()
        db_path, meta = deps.ensure_index(root, rebuild=rebuild)
        rows = _index_attr(deps, root, "search_index", deps.search_index)(
            db_path,
            query,
            limit=limit,
            artifact_type=artifact_type or None,
        )
        rows = rows[:limit]
        event_id = deps.record_usage(
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
            index_metadata=meta,
        )
        return deps.tool_result(
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
        event_id = deps.record_usage(
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
            index_metadata=meta,
        )
        return deps.tool_error(message, success=False, usage_event_id=event_id)

def _handle_get(args: dict[str, Any], *, deps: HandlerDeps | None = None, **kwargs) -> str:
    deps = _handler_deps(deps)
    if error := _validate_args(args, deps):
        return error
    started = time.perf_counter()
    context = deps.usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return deps.tool_error("artifact_id is required", success=False)

    rebuild = deps.coerce_bool(args.get("rebuild"), default=False)
    include_neighbors = deps.coerce_bool(args.get("include_neighbors"), default=False)
    root: Path | None = None
    db_path: Path | None = None
    meta: dict[str, Any] = {}

    try:
        root = deps.repo_root()
        db_path, meta = deps.ensure_index(root, rebuild=rebuild)
        artifact = _index_attr(deps, root, "get_artifact", deps.get_artifact)(db_path, artifact_id)
        if artifact is None:
            message = f"Artifact not found: {artifact_id}"
            event_id = deps.record_usage(
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
                index_metadata=meta,
            )
            return deps.tool_error(
                message,
                success=False,
                artifact_id=artifact_id,
                usage_event_id=event_id,
                **meta,
            )
        neighbors = _index_attr(deps, root, "get_neighbors", deps.get_neighbors)(db_path, artifact_id) if include_neighbors else None
        event_id = deps.record_usage(
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
        return deps.tool_result(payload)
    except Exception as exc:
        message = f"knowledge_get failed: {type(exc).__name__}: {exc}"
        event_id = deps.record_usage(
            root,
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
        return deps.tool_error(message, success=False, usage_event_id=event_id)

def _handle_neighbors(args: dict[str, Any], *, deps: HandlerDeps | None = None, **kwargs) -> str:
    deps = _handler_deps(deps)
    if error := _validate_args(args, deps):
        return error
    started = time.perf_counter()
    context = deps.usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return deps.tool_error("artifact_id is required", success=False)

    limit = deps.coerce_int(args.get("limit"), default=20, minimum=1, maximum=50)
    rebuild = deps.coerce_bool(args.get("rebuild"), default=False)
    root: Path | None = None
    db_path: Path | None = None
    meta: dict[str, Any] = {}

    try:
        root = deps.repo_root()
        db_path, meta = deps.ensure_index(root, rebuild=rebuild)
        artifact = _index_attr(deps, root, "get_artifact", deps.get_artifact)(db_path, artifact_id)
        if artifact is None:
            message = f"Artifact not found: {artifact_id}"
            event_id = deps.record_usage(
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
                index_metadata=meta,
            )
            return deps.tool_error(
                message,
                success=False,
                artifact_id=artifact_id,
                usage_event_id=event_id,
                **meta,
            )
        rows = _index_attr(deps, root, "get_neighbors", deps.get_neighbors)(db_path, artifact_id)[:limit]
        event_id = deps.record_usage(
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
            index_metadata=meta,
        )
        return deps.tool_result(
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
        event_id = deps.record_usage(
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
            index_metadata=meta,
        )
        return deps.tool_error(message, success=False, usage_event_id=event_id)

def _handle_feedback(args: dict[str, Any], *, deps: HandlerDeps | None = None, **kwargs) -> str:
    deps = _handler_deps(deps)
    if error := _validate_args(args, deps):
        return error
    started = time.perf_counter()
    context = deps.usage_context(kwargs)
    rating = str(args.get("rating") or "").strip().lower()
    if rating not in FEEDBACK_RATINGS:
        return deps.tool_error(
            f"rating must be one of: {', '.join(sorted(FEEDBACK_RATINGS))}",
            success=False,
        )
    event_id_raw = args.get("event_id")
    try:
        event_id = int(event_id_raw) if event_id_raw is not None else None
    except Exception:
        return deps.tool_error("event_id must be an integer when provided", success=False)

    query = str(args.get("query") or "")
    artifact_id = str(args.get("artifact_id") or "")
    note = str(args.get("note") or "")
    root: Path | None = None
    try:
        root = deps.repo_root()
        feedback_id = deps.record_feedback(
            root,
            rating=rating,
            event_id=event_id,
            query=query,
            artifact_id=artifact_id,
            note=note,
            context=context,
        )
        usage_event_id = deps.record_usage(
            root,
            tool="knowledge_feedback",
            success=True,
            query=query,
            artifact_id=artifact_id,
            result_count=1,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=deps.usage_db_path(root),
            context=context,
        )
        return deps.tool_result(
            {
                "success": True,
                "feedback_id": feedback_id,
                "usage_event_id": usage_event_id,
                "rating": rating,
                "event_id": event_id,
                "usage_db_path": str(deps.usage_db_path(root)),
            }
        )
    except Exception as exc:
        message = f"knowledge_feedback failed: {type(exc).__name__}: {exc}"
        usage_event_id = deps.record_usage(
            root,
            tool="knowledge_feedback",
            success=False,
            query=query,
            artifact_id=artifact_id,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            context=context,
        )
        return deps.tool_error(message, success=False, usage_event_id=usage_event_id)

def _handle_usage_report(args: dict[str, Any], *, deps: HandlerDeps | None = None, **kwargs) -> str:
    deps = _handler_deps(deps)
    if error := _validate_args(args, deps):
        return error
    started = time.perf_counter()
    context = deps.usage_context(kwargs)
    days = deps.coerce_int(args.get("days"), default=14, minimum=1, maximum=365)
    limit = deps.coerce_int(args.get("limit"), default=10, minimum=1, maximum=50)
    root: Path | None = None
    try:
        root = deps.repo_root()
        report = deps.usage_report(root, days=days, limit=limit)
        usage_event_id = deps.record_usage(
            root,
            tool="knowledge_usage_report",
            success=True,
            limit_value=limit,
            result_count=int(report.get("total_events") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=deps.usage_db_path(root),
            context=context,
        )
        report["usage_event_id"] = usage_event_id
        return deps.tool_result(report)
    except Exception as exc:
        message = f"knowledge_usage_report failed: {type(exc).__name__}: {exc}"
        usage_event_id = deps.record_usage(
            root,
            tool="knowledge_usage_report",
            success=False,
            error=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            context=context,
        )
        return deps.tool_error(message, success=False, usage_event_id=usage_event_id)
