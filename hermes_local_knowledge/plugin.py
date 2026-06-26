"""Hermes plugin exposing a local capability index as native tools."""
from __future__ import annotations

from . import indexer
from .handlers import _handle_feedback, _handle_get, _handle_neighbors, _handle_search, _handle_usage_report
from .runtime import (
    RuntimeConfig,
    _coerce_bool,
    _coerce_int,
    _config_value,
    _db_path,
    _ensure_index,
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
