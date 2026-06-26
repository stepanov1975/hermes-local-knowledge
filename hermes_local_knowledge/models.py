"""Data models for local knowledge artifacts and index settings."""
from __future__ import annotations

from dataclasses import dataclass, field

from .constants import DEFAULT_KNOWN_ENTITIES


@dataclass(frozen=True)
class Artifact:
    id: str
    type: str
    title: str
    path: str
    summary: str
    triggers: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    updated_at: str | None = None
    source: str | None = None
    search_text: str = ""

@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str
    evidence: str

@dataclass(frozen=True)
class IndexSettings:
    """Scanner layout and ranking hints for a source tree.

    Paths are relative to the configured source root, except ``hermes_home``
    which is passed separately to build/search live Hermes runtime artifacts.
    """

    custom_skill_dirs: tuple[str, ...] = ("custom_skills",)
    script_dirs: tuple[str, ...] = ("scripts", "hermes_home/scripts")
    memory_dirs: tuple[str, ...] = ("memory",)
    runbook_dirs: tuple[str, ...] = ("docs", "main_docker_server")
    known_entities: tuple[str, ...] = tuple(DEFAULT_KNOWN_ENTITIES)
