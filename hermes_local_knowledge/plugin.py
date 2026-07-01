"""Hermes plugin exposing a local capability index as native tools."""
from __future__ import annotations

from pathlib import Path

from . import handlers as _handlers
from . import indexer
from .handlers import HandlerDeps
from .runtime import (
    RuntimeConfig,
    _coerce_bool,
    _coerce_int,
    _config_value,
    _db_path,
    _ensure_index as _runtime_ensure_index,
    _get_hermes_home,
    _index_module,
    _load_hermes_config,
    _output_dir,
    _path_value,
    _repo_root,
    _runtime_config,
    _section_config,
    _tuple_value,
    _usage_db_path,
    check_knowledge_available,
)
from .schemas import (
    CONFIG_SECTION,
    FEEDBACK_RATINGS,
    KNOWLEDGE_FEEDBACK_SCHEMA,
    KNOWLEDGE_GET_SCHEMA,
    KNOWLEDGE_NEIGHBORS_SCHEMA,
    KNOWLEDGE_SEARCH_SCHEMA,
    KNOWLEDGE_USAGE_REPORT_SCHEMA,
    NEGATIVE_FEEDBACK_RATINGS,
    ROOT_ENV,
    STATE_ENV,
    TOOLSET,
)
from .telemetry import (
    FEEDBACK_COLUMNS,
    USAGE_EVENT_COLUMNS,
    _clean_text,
    _ensure_columns,
    _init_usage_db,
    _json_list,
    _record_feedback,
    _record_usage,
    _rows,
    _usage_connect,
    _usage_context,
    _usage_report,
    _utc_now,
)
from .tooling import tool_error, tool_result

# Keep wrapper-level names explicit for legacy callers/tests that monkeypatch
# hermes_local_knowledge.plugin after the internal module split.
__all__ = [
    "CONFIG_SECTION",
    "FEEDBACK_COLUMNS",
    "FEEDBACK_RATINGS",
    "KNOWLEDGE_FEEDBACK_SCHEMA",
    "KNOWLEDGE_GET_SCHEMA",
    "KNOWLEDGE_NEIGHBORS_SCHEMA",
    "KNOWLEDGE_SEARCH_SCHEMA",
    "KNOWLEDGE_USAGE_REPORT_SCHEMA",
    "NEGATIVE_FEEDBACK_RATINGS",
    "ROOT_ENV",
    "RuntimeConfig",
    "STATE_ENV",
    "TOOLSET",
    "USAGE_EVENT_COLUMNS",
    "_clean_text",
    "_coerce_bool",
    "_coerce_int",
    "_config_value",
    "_db_path",
    "_ensure_columns",
    "_ensure_index",
    "_get_hermes_home",
    "_handle_feedback",
    "_handle_get",
    "_handle_neighbors",
    "_handle_search",
    "_handle_usage_report",
    "_index_module",
    "_init_usage_db",
    "_json_list",
    "_load_hermes_config",
    "_output_dir",
    "_path_value",
    "_record_feedback",
    "_record_usage",
    "_repo_root",
    "_rows",
    "_runtime_config",
    "_section_config",
    "_tuple_value",
    "_usage_connect",
    "_usage_context",
    "_usage_db_path",
    "_usage_report",
    "_utc_now",
    "check_knowledge_available",
    "indexer",
    "register",
    "tool_error",
    "tool_result",
]


def _ensure_index(root: Path, *, rebuild: bool = False):
    """Build through this compatibility module's index-module seam."""
    return _runtime_ensure_index(
        root,
        rebuild=rebuild,
        build_index_fn=_index_module(root).build_index,
    )


def _handler_deps() -> HandlerDeps:
    """Resolve handler dependencies from this compatibility module's globals."""
    return HandlerDeps(
        coerce_bool=_coerce_bool,
        coerce_int=_coerce_int,
        ensure_index=_ensure_index,
        index_module=_index_module,
        record_feedback=_record_feedback,
        record_usage=_record_usage,
        repo_root=_repo_root,
        tool_error=tool_error,
        tool_result=tool_result,
        usage_context=_usage_context,
        usage_db_path=_usage_db_path,
        usage_report=_usage_report,
    )


def _handle_search(args, **kwargs) -> str:
    return _handlers._handle_search(args, deps=_handler_deps(), **kwargs)


def _handle_get(args, **kwargs) -> str:
    return _handlers._handle_get(args, deps=_handler_deps(), **kwargs)


def _handle_neighbors(args, **kwargs) -> str:
    return _handlers._handle_neighbors(args, deps=_handler_deps(), **kwargs)


def _handle_feedback(args, **kwargs) -> str:
    return _handlers._handle_feedback(args, deps=_handler_deps(), **kwargs)


def _handle_usage_report(args, **kwargs) -> str:
    return _handlers._handle_usage_report(args, deps=_handler_deps(), **kwargs)


def register(ctx) -> None:
    """Register native tools for the local knowledge index."""
    for name, schema, handler, emoji in (
        ("knowledge_search", KNOWLEDGE_SEARCH_SCHEMA, _handle_search, "🗺️"),
        ("knowledge_get", KNOWLEDGE_GET_SCHEMA, _handle_get, "📄"),
        ("knowledge_neighbors", KNOWLEDGE_NEIGHBORS_SCHEMA, _handle_neighbors, "🔗"),
        ("knowledge_feedback", KNOWLEDGE_FEEDBACK_SCHEMA, _handle_feedback, "📝"),
        ("knowledge_usage_report", KNOWLEDGE_USAGE_REPORT_SCHEMA, _handle_usage_report, "📊"),
    ):
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=check_knowledge_available,
            emoji=emoji,
        )
