"""Hermes plugin exposing a local capability index as native tools."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from . import handlers as _handlers
from .okf import _on_post_tool_call, _on_session_finalize
from .runtime import _runtime_config, check_knowledge_available
from .service import LocalKnowledgeService

__all__ = ["register"]


def _service() -> LocalKnowledgeService:
    """Construct a service from the current resolved plugin configuration."""

    return LocalKnowledgeService(_runtime_config())


def _handle_search(args: Any, **kwargs: Any) -> str:
    return _handlers._handle_search(args, service_factory=_service, **kwargs)


def _handle_get(args: Any, **kwargs: Any) -> str:
    return _handlers._handle_get(args, service_factory=_service, **kwargs)


def _handle_neighbors(args: Any, **kwargs: Any) -> str:
    return _handlers._handle_neighbors(args, service_factory=_service, **kwargs)


def _handle_feedback(args: Any, **kwargs: Any) -> str:
    return _handlers._handle_feedback(args, service_factory=_service, **kwargs)


def _handle_usage_report(args: Any, **kwargs: Any) -> str:
    return _handlers._handle_usage_report(args, service_factory=_service, **kwargs)


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
