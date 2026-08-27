"""Hermes plugin exposing a local capability index as native tools."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any

from . import index
from .config import resolve_config
from .implicit import on_post_tool_call as _on_implicit_post_tool_call
from .implicit import on_pre_llm_call as _on_implicit_pre_llm_call
from .implicit import on_session_end as _on_implicit_session_end
from .okf import _on_post_tool_call as _on_okf_post_tool_call
from .okf import _on_session_finalize
from .routing import ROUTING_TRACE_METADATA_KEY, SearchRoutingTrace
from .service import LocalKnowledgeService
from .telemetry import FEEDBACK_RATINGS, FeedbackDatabaseLockedError, _usage_context

__all__ = ["register"]

KNOWLEDGE_SEARCH_HINT = (
    "For local Hermes/homelab skills, scripts, runbooks, cron jobs, MCP wrappers, or service "
    "docs, use `knowledge_search` before broad file search; verify live state directly."
)


def _render_search_hint(_session_info: Mapping[str, Any]) -> str:
    """Render the static hint only when local knowledge is configured."""

    return KNOWLEDGE_SEARCH_HINT if check_knowledge_available() else ""


def _bind_implicit_pre_llm_context(**kwargs: Any) -> None:
    """Bind implicit-feedback state without injecting static prompt text."""

    _on_implicit_pre_llm_call(**kwargs)


def _history_contains_search_hint(conversation_history: Any) -> bool:
    """Return whether the current API history already carries the hint."""

    if not isinstance(conversation_history, list):
        return False
    for message in conversation_history:
        if not isinstance(message, dict):
            continue
        for key in ("api_content", "content"):
            content = message.get(key)
            if isinstance(content, str) and KNOWLEDGE_SEARCH_HINT in content:
                return True
    return False


def _on_pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    """Bind implicit state and inject the hint on older Hermes hosts."""

    _bind_implicit_pre_llm_context(**kwargs)
    if not check_knowledge_available():
        return None
    if _history_contains_search_hint(kwargs.get("conversation_history")):
        return None
    return {"context": KNOWLEDGE_SEARCH_HINT}


def _on_post_tool_call(**kwargs: Any) -> None:
    _on_okf_post_tool_call(**kwargs)
    _on_implicit_post_tool_call(**kwargs)


def _service() -> LocalKnowledgeService:
    """Construct a service from the current resolved plugin configuration."""

    return LocalKnowledgeService(resolve_config())


def check_knowledge_available() -> bool:
    """Return whether the configured source and Hermes home are available."""

    try:
        config = resolve_config()
        return config.source_root.is_dir() and config.hermes_home.is_dir()
    except Exception:
        return False


def _tool_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


_URL_USERINFO_PATTERN = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://)([^/@\s\"\\]+)@"
)
_CREDENTIAL_KEY = (
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|pwd|secret|token)"
)
_CREDENTIAL_DOUBLE_QUOTED_PATTERN = re.compile(
    rf"(?i)((?:\\?\")?{_CREDENTIAL_KEY}(?:\\?\")?\s*[:=]\s*\\?\")"
    r"([^\"\\]*)(\\?\")"
)
_CREDENTIAL_SINGLE_QUOTED_PATTERN = re.compile(
    rf"(?i)((?:\\?\")?{_CREDENTIAL_KEY}(?:\\?\")?\s*[:=]\s*')([^']*)(')"
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)(\b{_CREDENTIAL_KEY}\s*[:=]\s*)([^\s,;&\"'\\]+)"
)
_AUTHORIZATION_VALUE_DOUBLE_QUOTED_PATTERN = re.compile(
    r"(?i)((?:\\?\")?authorization(?:\\?\")?\s*[:=]\s*\\?\")"
    r"([^\"\\]*)(\\?\")"
)
_AUTHORIZATION_VALUE_SINGLE_QUOTED_PATTERN = re.compile(
    r"(?i)((?:\\?\")?authorization(?:\\?\")?\s*[:=]\s*')([^']*)(')"
)
_AUTHORIZATION_DOUBLE_QUOTED_PATTERN = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic)\s+\\?\")"
    r"([^\"\\]*)(\\?\")"
)
_AUTHORIZATION_SINGLE_QUOTED_PATTERN = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic)\s+')([^']*)(')"
)
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic)\s+)([^\s,;&\"'\\]+)"
)


def _model_safe_usage_report_result(payload: dict[str, Any]) -> str:
    """Serialize a report while masking only obvious model-facing credentials."""

    serialized = json.dumps(payload, ensure_ascii=False)
    serialized = _URL_USERINFO_PATTERN.sub(r"\1<redacted>@", serialized)
    serialized = _CREDENTIAL_DOUBLE_QUOTED_PATTERN.sub(r"\1<redacted>\3", serialized)
    serialized = _CREDENTIAL_SINGLE_QUOTED_PATTERN.sub(r"\1<redacted>\3", serialized)
    serialized = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(r"\1<redacted>", serialized)
    serialized = _AUTHORIZATION_VALUE_DOUBLE_QUOTED_PATTERN.sub(
        r"\1<redacted>\3", serialized
    )
    serialized = _AUTHORIZATION_VALUE_SINGLE_QUOTED_PATTERN.sub(
        r"\1<redacted>\3", serialized
    )
    serialized = _AUTHORIZATION_DOUBLE_QUOTED_PATTERN.sub(r"\1<redacted>\3", serialized)
    serialized = _AUTHORIZATION_SINGLE_QUOTED_PATTERN.sub(r"\1<redacted>\3", serialized)
    return _AUTHORIZATION_PATTERN.sub(r"\1<redacted>", serialized)


def _tool_error(message: object, **extra: Any) -> str:
    return _tool_result(
        {"error": str(message), **{key: value for key, value in extra.items() if value is not None}}
    )


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


_AGENT_ARTIFACT_FIELDS = (
    "id",
    "type",
    "title",
    "path",
    "summary",
    "source",
    "updated_at",
    "related",
)
_AGENT_EDGE_FIELDS = ("edge_kind", "edge_evidence")


def _has_agent_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _prune_agent_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: pruned
            for key, item in value.items()
            if _has_agent_value(pruned := _prune_agent_value(item))
        }
    if isinstance(value, list):
        return [
            pruned
            for item in value
            if _has_agent_value(pruned := _prune_agent_value(item))
        ]
    return value


def _agent_artifact(row: Mapping[str, Any], *, include_edge: bool = False) -> dict[str, Any]:
    """Project an index row to metadata that helps the model choose its next source."""

    fields = _AGENT_ARTIFACT_FIELDS + (_AGENT_EDGE_FIELDS if include_edge else ())
    return {field: row[field] for field in fields if field in row and _has_agent_value(row[field])}


def _add_usage_event(payload: dict[str, Any], event_id: int | None) -> None:
    if event_id is not None:
        payload["usage_event_id"] = event_id


def _add_actionable_lookup_metadata(payload: dict[str, Any], metadata: Mapping[str, Any]) -> None:
    """Expose lookup lifecycle details only when they change what the agent should know."""

    if metadata.get("rebuilt") is True:
        payload["rebuilt"] = True
    warnings = metadata.get("warnings")
    if warnings:
        payload["warnings"] = warnings


def _nonempty_projection(source: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for field in fields:
        if field not in source:
            continue
        value = _prune_agent_value(source[field])
        if _has_agent_value(value):
            projected[field] = value
    return projected


def _project_rows(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        projected
        for row in value
        if isinstance(row, Mapping)
        and (projected := _nonempty_projection(row, fields))
    ]


def _agent_improvement_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    candidate_type = str(row.get("type") or "")
    if candidate_type == "zero_result_query":
        return {
            "type": candidate_type,
            **_nonempty_projection(row, ("query", "count", "last_seen")),
        }
    if candidate_type == "tool_error":
        return {
            "type": candidate_type,
            **_nonempty_projection(row, ("client", "tool", "error", "count", "last_seen")),
        }
    if candidate_type.startswith("feedback_") or candidate_type == "correction_candidate":
        query = row.get("effective_query") or row.get("query")
        candidate = {
            "type": candidate_type,
            "query": query,
            "feedback_id": row.get("id"),
            **_nonempty_projection(
                row,
                (
                    "rating",
                    "artifact_id",
                    "expected_artifact_id",
                    "note",
                    "linkage_quality",
                    "artifact_type",
                ),
            ),
        }
        candidate_kind = row.get("candidate_kind")
        if candidate_kind and candidate_kind != candidate_type:
            candidate["candidate_kind"] = candidate_kind
        return _prune_agent_value(candidate)
    return _nonempty_projection(row, ("type", "query", "tool", "error", "count"))


def _agent_usage_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep model-facing usage output focused on live quality and repair candidates."""

    search_quality_raw = report.get("current_native_search_quality")
    search_quality = (
        _nonempty_projection(
            search_quality_raw,
            (
                "count",
                "successes",
                "errors",
                "zero_results",
                "route_changes",
                "avg_latency_ms",
                "last_seen",
            ),
        )
        if isinstance(search_quality_raw, Mapping)
        else {}
    )
    if isinstance(search_quality_raw, Mapping):
        for source_field, target_field, fields in (
            ("top_queries", "top_queries", ("query", "count", "avg_results", "last_seen")),
            (
                "active_zero_result_queries",
                "zero_result_queries",
                ("query", "count", "last_seen"),
            ),
            ("errors_by_message", "errors_by_message", ("error", "count", "last_seen")),
        ):
            rows = _project_rows(search_quality_raw.get(source_field), fields)
            if rows:
                search_quality[target_field] = rows
        consumed_rank_raw = search_quality_raw.get("implicit_consumed_rank_lower_bound")
        if isinstance(consumed_rank_raw, Mapping) and consumed_rank_raw.get(
            "consumed_artifact_count"
        ):
            consumed_rank = _nonempty_projection(
                consumed_rank_raw,
                (
                    "consumed_artifact_count",
                    "consumed_search_count",
                    "ranked_consumption_count",
                    "unranked_consumption_count",
                    "consumed_at_rank_1",
                    "consumed_in_top_3",
                    "consumed_outside_top_3",
                    "searches_with_consumed_rank_1",
                    "searches_with_consumed_top_3",
                    "median_consumed_rank",
                ),
            )
            rank_distribution = _project_rows(
                consumed_rank_raw.get("rank_distribution"), ("rank", "count")
            )
            if rank_distribution:
                consumed_rank["rank_distribution"] = rank_distribution
            search_quality["implicit_consumed_rank_lower_bound"] = consumed_rank
    route_outcomes = (
        _project_rows(
            search_quality_raw.get("route_outcomes"),
            ("route_outcome", "count", "last_seen"),
        )
        if isinstance(search_quality_raw, Mapping)
        else []
    )
    if route_outcomes:
        search_quality["route_outcomes"] = route_outcomes
    route_failures = (
        search_quality_raw.get("route_verification_failures")
        if isinstance(search_quality_raw, Mapping)
        else None
    )
    if isinstance(route_failures, list):
        projected_failures = [
            _nonempty_projection(
                row,
                (
                    "query",
                    "artifact_type",
                    "route_feedback_id",
                    "route_artifact_id",
                    "route_outcome",
                ),
            )
            for row in route_failures
            if isinstance(row, Mapping)
        ]
        if projected_failures:
            search_quality["route_verification_failures"] = projected_failures

    feedback: dict[str, Any] = {
        "count": int(report.get("live_feedback_count") or 0),
        "implicit_count": int(report.get("live_implicit_feedback_count") or 0),
    }
    feedback_by_consumer = _project_rows(
        report.get("implicit_feedback_by_consumer"),
        ("consumer_tool", "count", "last_seen"),
    )
    if feedback_by_consumer:
        feedback["implicit_feedback_by_consumer"] = feedback_by_consumer
    replay_counts_raw = report.get("replay_ready_label_counts")
    replay_counts = (
        _nonempty_projection(
            replay_counts_raw,
            ("explicit_resolution", "verified_event", "direct_or_legacy", "total"),
        )
        if isinstance(replay_counts_raw, Mapping)
        else {}
    )
    if replay_counts.get("total"):
        feedback["replay_ready_label_counts"] = replay_counts

    candidates: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()

    def add_candidate(row: Mapping[str, Any]) -> None:
        candidate = _agent_improvement_candidate(row)
        if not candidate:
            return
        identity = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        if identity not in seen_candidates:
            seen_candidates.add(identity)
            candidates.append(candidate)

    for source_field in (
        "unresolved_negative_with_current_expected_target",
        "unresolved_negative_without_current_expected_target",
    ):
        feedback_rows = report.get(source_field)
        if isinstance(feedback_rows, list):
            for row in feedback_rows:
                if isinstance(row, Mapping):
                    add_candidate(row)

    if isinstance(search_quality_raw, Mapping):
        zero_rows = search_quality_raw.get("active_zero_result_queries")
        if isinstance(zero_rows, list):
            for row in zero_rows:
                if isinstance(row, Mapping):
                    add_candidate({"type": "zero_result_query", **row})
        error_rows = search_quality_raw.get("errors_by_message")
        if isinstance(error_rows, list):
            for row in error_rows:
                if isinstance(row, Mapping):
                    add_candidate(
                        {
                            "type": "tool_error",
                            "client": "native",
                            "tool": "knowledge_search",
                            **row,
                        }
                    )
    current_native_error_rows = report.get("current_native_errors")
    if isinstance(current_native_error_rows, list):
        for row in current_native_error_rows:
            if isinstance(row, Mapping):
                add_candidate({"type": "tool_error", "client": "native", **row})

    window = _nonempty_projection(report, ("days", "since"))
    payload: dict[str, Any] = {
        "success": bool(report.get("success", True)),
        "event_count": int(report.get("live_total_events") or 0),
        "feedback": feedback,
    }
    if window:
        payload["window"] = window
    if search_quality:
        payload["search_quality"] = search_quality
    event_cohorts = _project_rows(
        report.get("event_cohorts"),
        ("cohort", "count", "successes", "errors", "avg_latency_ms", "last_seen"),
    )
    if event_cohorts:
        payload["event_cohorts"] = event_cohorts
    if candidates:
        payload["improvement_candidates"] = candidates
    return payload


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
        routing_trace = meta.pop(ROUTING_TRACE_METADATA_KEY, None)
        rows = rows[:limit]
        final_ids = [str(row.get("id")) for row in rows]
        if isinstance(routing_trace, SearchRoutingTrace):
            baseline_ids = list(routing_trace.baseline_ids)
            route_decision = routing_trace.decision
            route_feedback_id = route_decision.feedback_id
            route_artifact_id = route_decision.artifact_id
            route_outcome = route_decision.outcome.value
            feedback_max_id = route_decision.feedback_max_id
            implicit_feedback_max_id = route_decision.implicit_feedback_max_id
        else:
            baseline_ids = final_ids
            route_feedback_id = None
            route_artifact_id = None
            route_outcome = "none"
            feedback_max_id = None
            implicit_feedback_max_id = None
        implicit_settings = getattr(getattr(service, "config", None), "implicit_feedback", None)
        event_id = service.record_usage(
            tool="knowledge_search",
            success=True,
            query=query,
            artifact_type=artifact_type,
            limit_value=limit,
            rebuild_requested=rebuild,
            rebuilt=bool(meta.get("rebuilt")),
            result_count=len(rows),
            top_ids=final_ids,
            top_types=[str(row.get("type")) for row in rows],
            baseline_top_ids=baseline_ids,
            route_feedback_id=route_feedback_id,
            route_artifact_id=route_artifact_id,
            route_outcome=route_outcome,
            feedback_max_id=feedback_max_id,
            implicit_feedback_max_id=implicit_feedback_max_id,
            implicit_feedback_enabled=(
                None if implicit_settings is None else implicit_settings.enabled
            ),
            implicit_min_confirmations=(
                None if implicit_settings is None else implicit_settings.min_confirmations
            ),
            implicit_max_generic_queries=(
                None if implicit_settings is None else implicit_settings.max_generic_queries
            ),
            latency_ms=_latency_ms(started),
            db_path=db_path,
            context=context,
            index_metadata=meta,
        )
        payload: dict[str, Any] = {
            "success": True,
            "results": [_agent_artifact(row) for row in rows],
        }
        _add_usage_event(payload, event_id)
        _add_actionable_lookup_metadata(payload, meta)
        return _tool_result(payload)
    except Exception as exc:
        message = f"knowledge_search failed: {type(exc).__name__}: {exc}"
        _record_handler_usage(
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
            error_payload = {"error": message, "success": False, "artifact_id": artifact_id}
            _add_usage_event(error_payload, event_id)
            _add_actionable_lookup_metadata(error_payload, meta)
            return _tool_result(error_payload)
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
            "artifact": _agent_artifact(artifact),
        }
        _add_usage_event(payload, event_id)
        _add_actionable_lookup_metadata(payload, meta)
        if neighbors is not None:
            payload["neighbors"] = [
                _agent_artifact(row, include_edge=True) for row in neighbors
            ]
        return _tool_result(payload)
    except Exception as exc:
        message = f"knowledge_get failed: {type(exc).__name__}: {exc}"
        _record_handler_usage(
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
            error_payload = {"error": message, "success": False, "artifact_id": artifact_id}
            _add_usage_event(error_payload, event_id)
            _add_actionable_lookup_metadata(error_payload, meta)
            return _tool_result(error_payload)
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
        payload: dict[str, Any] = {
            "success": True,
            "neighbors": [_agent_artifact(row, include_edge=True) for row in rows],
        }
        _add_usage_event(payload, event_id)
        _add_actionable_lookup_metadata(payload, meta)
        return _tool_result(payload)
    except Exception as exc:
        message = f"knowledge_neighbors failed: {type(exc).__name__}: {exc}"
        _record_handler_usage(
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
        feedback_id, _usage_event_id = service.feedback(
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
        return _tool_result({"success": True, "feedback_id": feedback_id})
    except Exception as exc:
        message = f"knowledge_feedback failed: {type(exc).__name__}: {exc}"
        # A lock failure already consumed the handler's bounded wait. Opening a
        # second connection would double that budget. Other argument/schema
        # failures retain the established best-effort failure event.
        if not isinstance(exc, FeedbackDatabaseLockedError):
            _record_handler_usage(
                service,
                tool="knowledge_feedback",
                success=False,
                query=query,
                artifact_id=artifact_id,
                error=message,
                latency_ms=_latency_ms(started),
                context=context,
            )
        return _tool_error(message, success=False)


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
        service.record_usage(
            tool="knowledge_usage_report",
            success=True,
            limit_value=limit,
            result_count=int(report.get("total_events") or 0),
            latency_ms=_latency_ms(started),
            db_path=service.usage_db_path,
            context=context,
        )
        return _model_safe_usage_report_result(_agent_usage_report(report))
    except Exception as exc:
        message = f"knowledge_usage_report failed: {type(exc).__name__}: {exc}"
        _record_handler_usage(
            service,
            tool="knowledge_usage_report",
            success=False,
            error=message,
            latency_ms=_latency_ms(started),
            context=context,
        )
        return _tool_error(message, success=False)


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
                    "Fetch concise routing metadata for one artifact from the local capability "
                    "index by id. Use after knowledge_search returns an artifact id. Usage is "
                    "logged locally."
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
                    "Return a concise summary of local knowledge usage, search quality, "
                    "feedback, and repair candidates. Use before changing index ranking, "
                    "triggers, docs, or graph edges."
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
    pre_llm_callback = _on_pre_llm_call
    register_system_prompt_section = getattr(ctx, "register_system_prompt_section", None)
    if callable(register_system_prompt_section):
        register_system_prompt_section(
            "local-knowledge.discovery",
            _render_search_hint,
            position="after_memory",
            max_chars=200,
        )
        pre_llm_callback = _bind_implicit_pre_llm_context
    register_hook = getattr(ctx, "register_hook", None)
    if register_hook is not None:
        register_hook("pre_llm_call", pre_llm_callback)
        register_hook("post_tool_call", _on_post_tool_call)
        register_hook("on_session_end", _on_implicit_session_end)
        register_hook("on_session_finalize", _on_session_finalize)
