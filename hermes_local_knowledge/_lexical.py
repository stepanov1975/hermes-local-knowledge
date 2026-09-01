"""Shared lexical policy for artifact collection and search routing."""

from __future__ import annotations

import getpass


def _runtime_stopwords() -> set[str]:
    try:
        username = getpass.getuser().strip().lower()
    except Exception:
        return set()
    return {username} if len(username) >= 3 else set()


COMMON_STOPWORDS = frozenset(
    {
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
    }
    | _runtime_stopwords()
)
