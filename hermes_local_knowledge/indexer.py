#!/usr/bin/env python3
"""Public compatibility facade and CLI entry point for local knowledge."""
from __future__ import annotations

from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct script execution compatibility
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "hermes_local_knowledge"

from .artifacts import Artifact, Edge
from .cli import main
from .config import IndexSettings
from .index import build_index, get_artifact, get_neighbors, search_index

__all__ = [
    "Artifact",
    "Edge",
    "IndexSettings",
    "build_index",
    "search_index",
    "get_artifact",
    "get_neighbors",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
