#!/usr/bin/env python3
"""Build and query a local Hermes capability index.

The index is an artifact router: it helps an agent discover which local skill,
script, runbook, cron job, MCP wrapper, or operational document to inspect
before doing broad search or guessing paths. It intentionally indexes whole
artifacts, not arbitrary text chunks.

This module is kept as the public compatibility surface and CLI entry point;
implementation lives in focused submodules.
"""
from __future__ import annotations

if __package__ in (None, ""):  # pragma: no cover - direct script execution compatibility
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from hermes_local_knowledge.cli import add_common_db_arg, main, parse_args, print_results
from hermes_local_knowledge.constants import (
    DEFAULT_KNOWN_ENTITIES,
    DEFAULT_ROOT,
    DEFAULT_STATE_DIR_NAME,
    EXCLUDED_DIR_NAMES,
    QUERY_STOPWORDS,
    SCRIPT_SUFFIXES,
    STOPWORDS,
)
from hermes_local_knowledge.models import Artifact, Edge, IndexSettings
from hermes_local_knowledge.paths import (
    default_output_dir,
    display_path,
    hermes_home_from_env,
    is_within_allowed_roots,
    iter_files_followlinks,
    path_is_relative_to,
    repo_root,
    should_skip_path,
    stat_key,
)
from hermes_local_knowledge.scanners import (
    build_edges,
    collect_artifacts,
    dedupe_edges,
    doc_type_for_path,
    load_json,
    load_yaml_if_available,
    parse_mcp_servers_fallback,
    resolve_related,
    scan_cron_jobs,
    scan_markdown_docs,
    scan_mcp_servers,
    scan_scripts,
    scan_skills,
    script_summary,
    skill_support_file_names,
)
from hermes_local_knowledge.search import search_index
from hermes_local_knowledge.storage import (
    build_index,
    build_sqlite,
    connect_readonly,
    decode_artifact_row,
    get_artifact,
    get_neighbors,
    write_jsonl,
)
from hermes_local_knowledge.text_utils import (
    extract_entities,
    extract_paths,
    first_heading_or_paragraph,
    first_sentence,
    fts_query,
    normalize_query_term,
    parse_bracket_list,
    parse_frontmatter,
    query_terms,
    regex_list_after_key,
    relative_config_parts,
    relpath_matches_config_dir,
    safe_read_text,
    search_sort_key,
    significant_words,
    slugify,
    token_hits,
    type_priority,
    unique_preserve_order,
)

__all__ = [
    "Artifact",
    "DEFAULT_KNOWN_ENTITIES",
    "DEFAULT_ROOT",
    "DEFAULT_STATE_DIR_NAME",
    "EXCLUDED_DIR_NAMES",
    "Edge",
    "IndexSettings",
    "QUERY_STOPWORDS",
    "SCRIPT_SUFFIXES",
    "STOPWORDS",
    "add_common_db_arg",
    "build_edges",
    "build_index",
    "build_sqlite",
    "collect_artifacts",
    "connect_readonly",
    "decode_artifact_row",
    "dedupe_edges",
    "default_output_dir",
    "display_path",
    "doc_type_for_path",
    "extract_entities",
    "extract_paths",
    "first_heading_or_paragraph",
    "first_sentence",
    "fts_query",
    "get_artifact",
    "get_neighbors",
    "hermes_home_from_env",
    "is_within_allowed_roots",
    "iter_files_followlinks",
    "load_json",
    "load_yaml_if_available",
    "main",
    "normalize_query_term",
    "parse_args",
    "parse_bracket_list",
    "parse_frontmatter",
    "parse_mcp_servers_fallback",
    "path_is_relative_to",
    "print_results",
    "query_terms",
    "regex_list_after_key",
    "relative_config_parts",
    "relpath_matches_config_dir",
    "repo_root",
    "resolve_related",
    "safe_read_text",
    "scan_cron_jobs",
    "scan_markdown_docs",
    "scan_mcp_servers",
    "scan_scripts",
    "scan_skills",
    "script_summary",
    "search_index",
    "search_sort_key",
    "should_skip_path",
    "significant_words",
    "skill_support_file_names",
    "slugify",
    "stat_key",
    "token_hits",
    "type_priority",
    "unique_preserve_order",
    "write_jsonl",
]


if __name__ == "__main__":
    raise SystemExit(main())
