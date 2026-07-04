"""Constants for the local knowledge indexer."""
from __future__ import annotations

import getpass
from pathlib import Path

DEFAULT_ROOT = Path.cwd()
DEFAULT_STATE_DIR_NAME = "local_knowledge"

SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".cjs", ".mjs", ".js"}
EXCLUDED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "htmlcov",
    "logs",
    "node_modules",
    "venv",
    ".venv",
    "worktrees",
    ".worktrees",
}
"""Default directory names excluded from indexing.

Users can extend this set at runtime via the ``exclude_dir_names`` config key
(see ``local_knowledge.exclude_dir_names`` in config.yaml). The effective
excluded set is the union of these defaults and any user-supplied names.
"""
DEFAULT_KNOWN_ENTITIES = ["Hermes", "GitHub", "MCP", "Cron"]

def _runtime_stopwords() -> set[str]:
    try:
        username = getpass.getuser().strip().lower()
    except Exception:
        return set()
    if len(username) < 3:
        return set()
    return {username}

STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "agent",
    "and",
    "are",
    "before",
    "build",
    "can",
    "code",
    "config",
    "data",
    "default",
    "doc",
    "docs",
    "file",
    "files",
    "for",
    "from",
    "has",
    "have",
    "hermes",
    "into",
    "local",
    "markdown",
    "not",
    "note",
    "repo",
    "review",
    "run",
    "script",
    "server",
    "skill",
    "that",
    "the",
    "this",
    "tool",
    "tools",
    "use",
    "using",
    "when",
    "with",
} | _runtime_stopwords()

QUERY_STOPWORDS = {
    "find",
    "flow",
    "markdown",
    "need",
    "next",
    "show",
    "want",
    "what",
    "where",
    "which",
}
