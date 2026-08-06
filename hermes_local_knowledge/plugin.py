"""Hermes plugin exposing a local capability index as native tools."""

from __future__ import annotations

import json
import time
from functools import partial
from pathlib import Path
from typing import Any

from . import index
from .config import resolve_config
from .okf import _on_post_tool_call, _on_session_finalize
from .service import LocalKnowledgeService
from .telemetry import FEEDBACK_RATINGS, FeedbackDatabaseLockedError, _usage_context

__all__ = ["register"]


def _service() -> LocalKnowledgeService:
    """Construct a service from the current resolved plugin configuration."""

    return LocalKnowledgeService(resolve_config())


def check_knowledge_available() -> bool:
    """Return whether the configured source and Hermes home are available."""

    try:
        config = resolve_config()
        return config.source_root.exists() and config.hermes_home.exists()
    except Exception:
        return False


def _tool_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _tool_error(message: object, **extra: Any) -> str:
    return _tool_result({"error": str(message), **extra})


def _validate_args(args: Any) -> str | None:
    if isinstance(args, dict):
        return None
    return _tool_error("args must be an object", success=False)


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _exception_fields(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, index.NewerIndexFormatError):
        return {
            "error_code": "newer_index_format",
            "expected_index_format_version": exc.expected_version,
            "actual_index_format_version": exc.actual_version,
        }
    return {}


def _latency_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _record_handler_usage(
    service: LocalKnowledgeService | None,
    **kwargs: Any,
) -> int | None:
    return service.record_usage(**kwargs) if service is not None else None


def _handle_search(args: Any, **kwargs: Any) -> str:
    if error := _validate_args(args):
        return error
    started = time.perf_counter()
    context = _usage_context(kwargs)
    query = str(args.get("query") or "").strip()
    if not query:
        return _tool_error("query is required", success=False)

    limit = _coerce_int(args.get("limit"), default=8, minimum=1, maximum=30)
    artifact_type = str(args.get("artifact_type") or "").strip()
    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    service: LocalKnowledgeService | None = None
    db_path: Path | None = None
    meta: dict[str, Any] = {}

    try:
        service = _service()
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
            top_ids=[str(row.get("id")) for row in rows],
            top_types=[str(row.get("type")) for row in rows],
            latency_ms=_latency_ms(started),
            db_path=db_path,
            context=context,
            index_metadata=meta,
        )
        return _tool_result(
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
            latency_ms=_latency_ms(started),
            db_path=db_path,
            context=context,
            index_metadata=meta,
        )
        return _tool_error(
            message,
            success=False,
            usage_event_id=event_id,
            **_exception_fields(exc),
        )


def _handle_get(args: Any, **kwargs: Any) -> str:
    if error := _validate_args(args):
        return error
    started = time.perf_counter()
    context = _usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return _tool_error("artifact_id is required", success=False)

    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    include_neighbors = _coerce_bool(args.get("include_neighbors"), default=False)
    service: LocalKnowledgeService | None = None
    db_path: Path | None = None
    meta: dict[str, Any] = {}

    try:
        service = _service()
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
                latency_ms=_latency_ms(started),
                db_path=db_path,
                context=context,
                index_metadata=meta,
            )
            return _tool_error(
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
            latency_ms=_latency_ms(started),
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
        return _tool_result(payload)
    except Exception as exc:
        message = f"knowledge_get failed: {type(exc).__name__}: {exc}"
        event_id = _record_handler_usage(
            service,
            tool="knowledge_get",
            success=False,
            artifact_id=artifact_id,
            rebuild_requested=rebuild,
            error=message,
            latency_ms=_latency_ms(started),
            db_path=db_path,
            context=context,
            index_metadata=meta,
        )
        return _tool_error(
            message,
            success=False,
            usage_event_id=event_id,
            **_exception_fields(exc),
        )


def _handle_neighbors(args: Any, **kwargs: Any) -> str:
    if error := _validate_args(args):
        return error
    started = time.perf_counter()
    context = _usage_context(kwargs)
    artifact_id = str(args.get("artifact_id") or "").strip()
    if not artifact_id:
        return _tool_error("artifact_id is required", success=False)

    limit = _coerce_int(args.get("limit"), default=20, minimum=1, maximum=50)
    rebuild = _coerce_bool(args.get("rebuild"), default=False)
    service: LocalKnowledgeService | None = None
    db_path: Path | None = None
    meta: dict[str, Any] = {}

    try:
        service = _service()
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
                latency_ms=_latency_ms(started),
                db_path=db_path,
                context=context,
                index_metadata=meta,
            )
            return _tool_error(
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
            top_ids=[str(row.get("id")) for row in rows],
            top_types=[str(row.get("type")) for row in rows],
            latency_ms=_latency_ms(started),
            db_path=db_path,
            context=context,
            index_metadata=meta,
        )
        return _tool_result(
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
            latency_ms=_latency_ms(started),
            db_path=db_path,
            context=context,
            index_metadata=meta,
        )
        return _tool_error(
            message,
            success=False,
            usage_event_id=event_id,
            **_exception_fields(exc),
        )


def _handle_feedback(args: Any, **kwargs: Any) -> str:
    if error := _validate_args(args):
        return error
    started = time.perf_counter()
    context = _usage_context(kwargs)
    rating = str(args.get("rating") or "").strip().lower()
    if rating not in FEEDBACK_RATINGS:
        return _tool_error(
            f"rating must be one of: {', '.join(sorted(FEEDBACK_RATINGS))}",
            success=False,
        )
    event_id_raw = args.get("event_id")
    try:
        event_id = int(event_id_raw) if event_id_raw is not None else None
    except Exception:
        return _tool_error("event_id must be an integer when provided", success=False)

    query = str(args.get("query") or "")
    artifact_id = str(args.get("artifact_id") or "")
    note = str(args.get("note") or "")
    expected_artifact_id = str(args.get("expected_artifact_id") or "")
    resolves_feedback_id_raw = args.get("resolves_feedback_id")
    try:
        resolves_feedback_id = (
            int(resolves_feedback_id_raw)
            if resolves_feedback_id_raw is not None
            else None
        )
    except Exception:
        return _tool_error(
            "resolves_feedback_id must be an integer when provided",
            success=False,
        )
    service: LocalKnowledgeService | None = None
    try:
        service = _service()
        feedback_id, usage_event_id = service.feedback(
            rating=rating,
            event_id=event_id,
            query=query,
            artifact_id=artifact_id,
            note=note,
            context=context,
            expected_artifact_id=expected_artifact_id,
            resolves_feedback_id=resolves_feedback_id,
            usage_started_at=started,
        )
        return _tool_result(
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
        # A lock failure already consumed the handler's bounded wait. Opening a
        # second connection would double that budget. Other argument/schema
        # failures retain the established best-effort failure event.
        failed_usage_event_id: int | None = None
        if not isinstance(exc, FeedbackDatabaseLockedError):
            failed_usage_event_id = _record_handler_usage(
                service,
                tool="knowledge_feedback",
                success=False,
                query=query,
                artifact_id=artifact_id,
                error=message,
                latency_ms=_latency_ms(started),
                context=context,
            )
        return _tool_error(
            message,
            success=False,
            usage_event_id=failed_usage_event_id,
        )


def _handle_usage_report(args: Any, **kwargs: Any) -> str:
    if error := _validate_args(args):
        return error
    started = time.perf_counter()
    context = _usage_context(kwargs)
    days = _coerce_int(args.get("days"), default=14, minimum=1, maximum=365)
    limit = _coerce_int(args.get("limit"), default=10, minimum=1, maximum=50)
    service: LocalKnowledgeService | None = None
    try:
        service = _service()
        report = service.usage_report(days=days, limit=limit)
        usage_event_id = service.record_usage(
            tool="knowledge_usage_report",
            success=True,
            limit_value=limit,
            result_count=int(report.get("total_events") or 0),
            latency_ms=_latency_ms(started),
            db_path=service.usage_db_path,
            context=context,
        )
        report["usage_event_id"] = usage_event_id
        return _tool_result(report)
    except Exception as exc:
        message = f"knowledge_usage_report failed: {type(exc).__name__}: {exc}"
        usage_event_id = _record_handler_usage(
            service,
            tool="knowledge_usage_report",
            success=False,
            error=message,
            latency_ms=_latency_ms(started),
            context=context,
        )
        return _tool_error(message, success=False, usage_event_id=usage_event_id)


def _bundled_router_skill() -> Path:
    """Return the plugin-local router skill path.

    Directory-plugin installs keep skills at the plugin root. Wheel installs use
    package data because top-level repo files are not import-addressable.
    """

    root_skill = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "local-knowledge-router"
        / "SKILL.md"
    )
    if root_skill.exists():
        return root_skill
    return (
        Path(__file__).resolve().parent
        / "skills"
        / "local-knowledge-router"
        / "SKILL.md"
    )


def _register_bundled_skills(ctx: Any) -> None:
    register_skill = getattr(ctx, "register_skill", None)
    if register_skill is None:
        return
    skill_md = _bundled_router_skill()
    if skill_md.exists():
        register_skill("local-knowledge-router", skill_md)


def _register_cli(ctx: Any) -> None:
    register_cli_command = getattr(ctx, "register_cli_command", None)
    if register_cli_command is None:
        return
    from .cli import handle_hermes_cli, setup_hermes_cli

    register_cli_command(
        name="local-knowledge",
        help="Install and diagnose the local knowledge plugin",
        description="Install the proactive router skill or check plugin health.",
        setup_fn=setup_hermes_cli,
        handler_fn=partial(handle_hermes_cli, llm=getattr(ctx, "llm", None)),
    )


def register(ctx: Any) -> None:
    """Register native tools and bundled skills for the local knowledge index."""

    for name, schema, handler, emoji in (
        (
            "knowledge_search",
            {
                "name": "knowledge_search",
                "description": (
                    "Search a local capability index to find the right local skill, "
                    "script, runbook, cron job, MCP wrapper, or service doc to inspect first. "
                    "Use this before broad file search for local Hermes customizations, "
                    "service-operation docs, cron jobs, MCP servers, or project runbooks. Builds "
                    "the index automatically when missing. Usage is logged locally for "
                    "closed-loop router improvement."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Natural-language search query, e.g. 'backup runbook' "
                                "or 'mcp wrapper'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 30,
                            "description": "Maximum results to return. Default 8, max 30.",
                        },
                        "artifact_type": {
                            "type": "string",
                            "description": (
                                "Optional type filter such as skill, script, runbook, "
                                "memory_doc, cron_job, mcp_server, doc, or "
                                "skill_support_doc."
                            ),
                        },
                        "rebuild": {
                            "type": "boolean",
                            "description": (
                                "Force a rebuild of the configured state_dir/index.sqlite "
                                "before searching. Default false."
                            ),
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            _handle_search,
            "🗺️",
        ),
        (
            "knowledge_get",
            {
                "name": "knowledge_get",
                "description": (
                    "Fetch one artifact from the local capability index by id, including "
                    "its path, summary, triggers, entities, and related artifact ids. Use after "
                    "knowledge_search returns an artifact id. Usage is logged locally."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {
                            "type": "string",
                            "description": (
                                "Artifact id such as skill:backup-runbook or "
                                "cron:daily-review."
                            ),
                        },
                        "include_neighbors": {
                            "type": "boolean",
                            "description": (
                                "Also include graph neighbors for this artifact. Default false."
                            ),
                        },
                        "rebuild": {
                            "type": "boolean",
                            "description": (
                                "Force a rebuild of the configured state_dir/index.sqlite "
                                "before reading. Default false."
                            ),
                        },
                    },
                    "required": ["artifact_id"],
                    "additionalProperties": False,
                },
            },
            _handle_get,
            "📄",
        ),
        (
            "knowledge_neighbors",
            {
                "name": "knowledge_neighbors",
                "description": (
                    "Return graph neighbors for one local capability artifact. Useful for "
                    "jumping from cron jobs to scripts, MCP config entries to wrappers, or "
                    "skills to related docs/scripts. Usage is logged locally."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {
                            "type": "string",
                            "description": (
                                "Artifact id from knowledge_search or knowledge_get."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "description": (
                                "Maximum neighbors to return. Default 20, max 50."
                            ),
                        },
                        "rebuild": {
                            "type": "boolean",
                            "description": (
                                "Force a rebuild of the configured state_dir/index.sqlite "
                                "before reading. Default false."
                            ),
                        },
                    },
                    "required": ["artifact_id"],
                    "additionalProperties": False,
                },
            },
            _handle_neighbors,
            "🔗",
        ),
        (
            "knowledge_feedback",
            {
                "name": "knowledge_feedback",
                "description": (
                    "Record feedback about a local knowledge lookup so future sessions can "
                    "improve the capability index. Call this when a result is useful, stale, "
                    "missing, noisy, or pointed at the wrong artifact."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rating": {
                            "type": "string",
                            "enum": [
                                "missing",
                                "noisy",
                                "not_useful",
                                "other",
                                "stale",
                                "useful",
                                "wrong_artifact",
                            ],
                            "description": (
                                "Feedback rating: useful, not_useful, missing, noisy, "
                                "wrong_artifact, stale, or other."
                            ),
                        },
                        "event_id": {
                            "type": "integer",
                            "description": (
                                "Optional usage_event_id returned by "
                                "knowledge_search/get/neighbors."
                            ),
                        },
                        "query": {
                            "type": "string",
                            "description": (
                                "Search query being judged, if no event_id is available."
                            ),
                        },
                        "artifact_id": {
                            "type": "string",
                            "description": "Artifact id being judged, if applicable.",
                        },
                        "note": {
                            "type": "string",
                            "description": (
                                "Short concrete note about what worked or what should improve. "
                                "Do not include secrets."
                            ),
                        },
                        "expected_artifact_id": {
                            "type": "string",
                            "description": (
                                "Verified artifact id that should have been returned for this "
                                "query. Use only after confirming the artifact exists. When set "
                                "on a negative parent, a resolution must accept this artifact."
                            ),
                        },
                        "resolves_feedback_id": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Feedback id of an unresolved negative parent to close. The new "
                                "rating must be useful, event_id must reference a successful "
                                "knowledge_search whose returned page contains artifact_id, and "
                                "the query is canonicalized to that search event."
                            ),
                        },
                    },
                    "required": ["rating"],
                    "additionalProperties": False,
                },
            },
            _handle_feedback,
            "📝",
        ),
        (
            "knowledge_usage_report",
            {
                "name": "knowledge_usage_report",
                "description": (
                    "Summarize local knowledge tool usage and feedback to guide "
                    "self-improvement. Use before changing index ranking, triggers, docs, "
                    "or graph edges."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 365,
                            "description": "Lookback window in days. Default 14.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "description": (
                                "Maximum rows per report section. Default 10."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            },
            _handle_usage_report,
            "📊",
        ),
    ):
        ctx.register_tool(
            name=name,
            toolset="local_knowledge",
            schema=schema,
            handler=handler,
            check_fn=check_knowledge_available,
            emoji=emoji,
        )
    _register_bundled_skills(ctx)
    _register_cli(ctx)
    register_hook = getattr(ctx, "register_hook", None)
    if register_hook is not None:
        register_hook("post_tool_call", _on_post_tool_call)
        register_hook("on_session_finalize", _on_session_finalize)
