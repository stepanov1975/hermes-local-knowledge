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

from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct script execution compatibility
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "hermes_local_knowledge"

from .cli import add_common_db_arg, main as _cli_main, parse_args, print_results
from .constants import (
    DEFAULT_KNOWN_ENTITIES,
    DEFAULT_ROOT,
    DEFAULT_STATE_DIR_NAME,
    EXCLUDED_DIR_NAMES,
    QUERY_STOPWORDS,
    ROUTING_HINT_TERMS,
    SCRIPT_SUFFIXES,
    STOPWORDS,
)
from .models import Artifact, Edge, IndexSettings
from .evaluation import (
    SearchMetrics,
    artifact_ids,
    artifact_parent_equivalence_map,
    evaluate_index_against_feedback,
    evaluate_search_labels,
    load_positive_feedback_labels,
)
from .paths import (
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
from .scanners import (
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
    scan_runtime_skill_support_docs,
    scan_scripts,
    scan_skills,
    script_summary,
    skill_support_file_names,
)
from .search import search_index
from .storage import (
    build_sqlite,
    connect_readonly,
    decode_artifact_row,
    get_artifact,
    get_neighbors,
    write_jsonl,
)
from .text_utils import (
    extract_entities,
    extract_env_names,
    extract_paths,
    first_heading_or_paragraph,
    first_sentence,
    fts_query,
    high_signal_terms,
    identifier_terms,
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

REPO_SCRIPT = Path(__file__).resolve()
KNOWN_ENTITIES = DEFAULT_KNOWN_ENTITIES
DEFAULT_OUTPUT_DIR = DEFAULT_ROOT / DEFAULT_STATE_DIR_NAME


def build_index(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
) -> tuple[list[Artifact], list[Edge]]:
    """Build an index through the compatibility-module function seams.

    The implementation modules own normal runtime behavior, but this public
    wrapper intentionally calls names re-exported from this module so existing
    tests/tools that monkeypatch ``indexer.collect_artifacts`` or
    ``indexer.build_edges`` keep working after the module split.
    """
    artifacts = collect_artifacts(root, hermes_home, settings)
    edges = build_edges(artifacts)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "index.jsonl", artifacts)
    build_sqlite(output_dir / "index.sqlite", artifacts, edges)
    return artifacts, edges

def main(argv=None) -> int:
    """Run the CLI through this compatibility module's function seams."""
    return _cli_main(
        argv,
        build_index_fn=build_index,
        search_index_fn=search_index,
        get_artifact_fn=get_artifact,
        get_neighbors_fn=get_neighbors,
    )

__all__ = [
    "Artifact",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_KNOWN_ENTITIES",
    "DEFAULT_ROOT",
    "DEFAULT_STATE_DIR_NAME",
    "EXCLUDED_DIR_NAMES",
    "Edge",
    "IndexSettings",
    "KNOWN_ENTITIES",
    "QUERY_STOPWORDS",
    "REPO_SCRIPT",
    "ROUTING_HINT_TERMS",
    "SCRIPT_SUFFIXES",
    "STOPWORDS",
    "SearchMetrics",
    "add_common_db_arg",
    "artifact_ids",
    "artifact_parent_equivalence_map",
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
    "extract_env_names",
    "extract_paths",
    "evaluate_index_against_feedback",
    "evaluate_search_labels",
    "first_heading_or_paragraph",
    "first_sentence",
    "fts_query",
    "get_artifact",
    "get_neighbors",
    "hermes_home_from_env",
    "high_signal_terms",
    "identifier_terms",
    "is_within_allowed_roots",
    "iter_files_followlinks",
    "load_json",
    "load_positive_feedback_labels",
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
    "scan_runtime_skill_support_docs",
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
