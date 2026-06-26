"""Hermes tool schemas for the local knowledge plugin."""
from __future__ import annotations

from typing import Any


ROOT_ENV = "LOCAL_KNOWLEDGE_ROOT"
STATE_ENV = "LOCAL_KNOWLEDGE_STATE_DIR"
CONFIG_SECTION = "local_knowledge"
TOOLSET = "local_knowledge"

FEEDBACK_RATINGS = {
    "useful",
    "not_useful",
    "missing",
    "noisy",
    "wrong_artifact",
    "stale",
    "other",
}
NEGATIVE_FEEDBACK_RATINGS = FEEDBACK_RATINGS - {"useful", "other"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

KNOWLEDGE_SEARCH_SCHEMA: dict[str, Any] = {
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
                "description": "Natural-language search query, e.g. 'backup runbook' or 'mcp wrapper'.",
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
                    "Optional type filter such as skill, script, runbook, memory_doc, "
                    "cron_job, mcp_server, doc, or skill_support_doc."
                ),
            },
            "rebuild": {
                "type": "boolean",
                "description": "Force a rebuild of the configured state_dir/index.sqlite before searching. Default false.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

KNOWLEDGE_GET_SCHEMA: dict[str, Any] = {
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
                "description": "Artifact id such as skill:backup-runbook or cron:daily-review.",
            },
            "include_neighbors": {
                "type": "boolean",
                "description": "Also include graph neighbors for this artifact. Default false.",
            },
            "rebuild": {
                "type": "boolean",
                "description": "Force a rebuild of the configured state_dir/index.sqlite before reading. Default false.",
            },
        },
        "required": ["artifact_id"],
        "additionalProperties": False,
    },
}

KNOWLEDGE_NEIGHBORS_SCHEMA: dict[str, Any] = {
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
                "description": "Artifact id from knowledge_search or knowledge_get.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Maximum neighbors to return. Default 20, max 50.",
            },
            "rebuild": {
                "type": "boolean",
                "description": "Force a rebuild of the configured state_dir/index.sqlite before reading. Default false.",
            },
        },
        "required": ["artifact_id"],
        "additionalProperties": False,
    },
}

KNOWLEDGE_FEEDBACK_SCHEMA: dict[str, Any] = {
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
                "enum": sorted(FEEDBACK_RATINGS),
                "description": "Feedback rating: useful, not_useful, missing, noisy, wrong_artifact, stale, or other.",
            },
            "event_id": {
                "type": "integer",
                "description": "Optional usage_event_id returned by knowledge_search/get/neighbors.",
            },
            "query": {
                "type": "string",
                "description": "Search query being judged, if no event_id is available.",
            },
            "artifact_id": {
                "type": "string",
                "description": "Artifact id being judged, if applicable.",
            },
            "note": {
                "type": "string",
                "description": "Short concrete note about what worked or what should improve. Do not include secrets.",
            },
        },
        "required": ["rating"],
        "additionalProperties": False,
    },
}

KNOWLEDGE_USAGE_REPORT_SCHEMA: dict[str, Any] = {
    "name": "knowledge_usage_report",
    "description": (
        "Summarize local knowledge tool usage and feedback to guide self-improvement. "
        "Use before changing index ranking, triggers, docs, or graph edges."
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
                "description": "Maximum rows per report section. Default 10.",
            },
        },
        "additionalProperties": False,
    },
}
