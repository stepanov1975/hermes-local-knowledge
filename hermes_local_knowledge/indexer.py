#!/usr/bin/env python3
"""Build and query a local Hermes capability index.

The index is an artifact router: it helps an agent discover which local skill,
script, runbook, cron job, MCP wrapper, or operational document to inspect
before doing broad search or guessing paths. It intentionally indexes whole
artifacts, not arbitrary text chunks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_ROOT = Path.cwd()
DEFAULT_OUTPUT_DIR = DEFAULT_ROOT / "knowledge"

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
}
DEFAULT_KNOWN_ENTITIES = [
    "Hermes",
    "Paperless",
    "SiYuan",
    "Docker",
    "Hindsight",
    "Home Assistant",
    "Pangolin",
    "Jellyfin",
    "Vikunja",
    "Firefly",
    "LinkAce",
    "Heimdall",
    "Wazuh",
    "n8n",
    "Evolution",
    "GitHub",
    "Telegram",
    "MCP",
    "Cron",
    "Hadera",
]
STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "agent",
    "alex",
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


def repo_root() -> Path:
    return DEFAULT_ROOT


def hermes_home_from_env() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "artifact"


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = str(value).strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        output.append(clean)
    return output


def display_path(path: Path, *, root: Path | None = None) -> str:
    expanded = path.expanduser()
    if root is not None:
        try:
            return expanded.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    try:
        return "~/" + expanded.resolve().relative_to(Path.home()).as_posix()
    except ValueError:
        return expanded.as_posix()


def safe_read_text(path: Path, *, max_chars: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(max_chars)
    except OSError:
        return ""


def significant_words(*parts: str, limit: int = 30) -> list[str]:
    words: list[str] = []
    for part in parts:
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{2,}", part):
            lowered = word.strip("._-").lower()
            if len(lowered) < 3 or lowered in STOPWORDS:
                continue
            words.append(lowered)
    return unique_preserve_order(words)[:limit]


def extract_entities(*parts: str, known_entities: Sequence[str] | None = None) -> list[str]:
    haystack = "\n".join(parts).lower()
    entities_source = known_entities if known_entities is not None else DEFAULT_KNOWN_ENTITIES
    entities = [entity for entity in entities_source if entity.lower() in haystack]
    return unique_preserve_order(entities)


def first_heading_or_paragraph(text: str) -> str:
    in_frontmatter = False
    if text.startswith("---"):
        in_frontmatter = True
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if in_frontmatter:
            if line == "---":
                in_frontmatter = False
            continue
        if not line or line.startswith("---") or line.startswith("```"):
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        return re.sub(r"\s+", " ", line)[:500]
    return ""


def parse_bracket_list(value: str) -> list[str]:
    clean = value.strip()
    if clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1]
    return [item.strip().strip("'\"") for item in clean.split(",") if item.strip().strip("'\"")]


def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    frontmatter: dict[str, Any] = {}
    current_key: str | None = None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped or stripped.startswith("#"):
            continue
        list_item = re.match(r"^[-*]\s+(.+)$", stripped)
        if list_item and current_key:
            current_value = frontmatter.get(current_key)
            if isinstance(current_value, list):
                current_value.append(list_item.group(1).strip().strip("'\""))
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", stripped)
        if not match:
            continue
        key, value = match.groups()
        current_key = key
        value = value.strip()
        if not value:
            frontmatter[key] = []
        elif value.startswith("[") and value.endswith("]"):
            frontmatter[key] = parse_bracket_list(value)
        else:
            frontmatter[key] = value.strip("'\"")
    return frontmatter


def regex_list_after_key(text: str, key: str) -> list[str]:
    match = re.search(rf"^\s*{re.escape(key)}:\s*\[([^\]]*)\]", text, re.MULTILINE)
    if match:
        return parse_bracket_list(match.group(1))

    match = re.search(rf"^\s*{re.escape(key)}:\s*\n((?:\s+-\s+.+\n?)+)", text, re.MULTILINE)
    if not match:
        return []
    return [line.split("-", 1)[1].strip().strip("'\"") for line in match.group(1).splitlines() if "-" in line]


def skill_support_file_names(skill_dir: Path) -> list[str]:
    names: list[str] = []
    for subdir in ("references", "templates", "scripts", "assets"):
        root = skill_dir / subdir
        if not root.exists():
            continue
        for child in sorted(root.rglob("*")):
            if child.is_file() and not should_skip_path(child):
                names.append(child.relative_to(skill_dir).as_posix())
    return names[:50]


def should_skip_path(path: Path) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in path.parts)


def iter_files_followlinks(root: Path, filename: str | None = None, suffixes: set[str] | None = None) -> Iterable[Path]:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_DIR_NAMES)
        for file_name in sorted(filenames):
            path = Path(dirpath) / file_name
            if should_skip_path(path):
                continue
            if filename is not None and file_name != filename:
                continue
            if suffixes is not None and path.suffix not in suffixes:
                continue
            yield path


def scan_skills(root: Path, hermes_home: Path, settings: IndexSettings | None = None) -> list[Artifact]:
    settings = settings or IndexSettings()
    artifacts: dict[str, Artifact] = {}
    sources = [
        *((root / rel, "custom_skill_source") for rel in settings.custom_skill_dirs),
        (hermes_home / "skills", "runtime_skill"),
    ]
    for skill_root, source in sources:
        for skill_md in iter_files_followlinks(skill_root, filename="SKILL.md") or []:
            text = safe_read_text(skill_md)
            fm = parse_frontmatter(text)
            name = str(fm.get("name") or skill_md.parent.name).strip()
            description = str(fm.get("description") or first_heading_or_paragraph(text)).strip()
            tags = fm.get("tags") if isinstance(fm.get("tags"), list) else regex_list_after_key(text, "tags")
            related_names = regex_list_after_key(text, "related_skills")
            support_files = skill_support_file_names(skill_md.parent)
            skill_id = f"skill:{slugify(name)}"
            category_parts: list[str] = []
            try:
                category_parts = list(skill_md.parent.relative_to(skill_root).parts[:-1])
            except ValueError:
                category_parts = []
            title = name
            related = [f"skill:{slugify(item)}" for item in related_names]
            triggers = significant_words(name, description, " ".join(tags or []), " ".join(category_parts), " ".join(support_files))
            entities = extract_entities(
                name, description, " ".join(tags or []), skill_md.as_posix(), known_entities=settings.known_entities
            )
            artifact = Artifact(
                id=skill_id,
                type="skill",
                title=title,
                path=display_path(skill_md.parent, root=root),
                summary=description,
                triggers=triggers,
                entities=entities,
                related=unique_preserve_order(related),
                source=source,
                search_text="\n".join([description, " ".join(tags or []), " ".join(support_files)]),
            )
            # Prefer custom source over the runtime symlink/copy for the same skill.
            if skill_id not in artifacts or artifacts[skill_id].source != "custom_skill_source":
                artifacts[skill_id] = artifact
    return sorted(artifacts.values(), key=lambda item: item.id)


def script_summary(path: Path, text: str) -> str:
    docstring = re.search(r'^[ruRUfbFB]*(["\']{3})(.*?)\1', text, re.DOTALL | re.MULTILINE)
    if docstring:
        return re.sub(r"\s+", " ", docstring.group(2).strip())[:500]
    comment_lines: list[str] = []
    for raw_line in text.splitlines()[:30]:
        line = raw_line.strip()
        if line.startswith("#!") or not line:
            continue
        if line.startswith("#"):
            comment_lines.append(line.lstrip("#").strip())
            continue
        break
    if comment_lines:
        return re.sub(r"\s+", " ", " ".join(comment_lines))[:500]
    return f"Local script {path.name}"


def scan_scripts(root: Path, settings: IndexSettings | None = None) -> list[Artifact]:
    settings = settings or IndexSettings()
    artifacts: list[Artifact] = []
    script_roots = [root / rel for rel in settings.script_dirs]
    for script_root in script_roots:
        for path in iter_files_followlinks(script_root, suffixes=SCRIPT_SUFFIXES) or []:
            text = safe_read_text(path, max_chars=50_000)
            rel = path.relative_to(root)
            title = rel.as_posix()
            summary = script_summary(path, text)
            artifact_id = f"script:{slugify(rel.as_posix())}"
            triggers = significant_words(title, summary, " ".join(rel.parts))
            entities = extract_entities(title, summary, path.as_posix(), known_entities=settings.known_entities)
            artifacts.append(
                Artifact(
                    id=artifact_id,
                    type="script",
                    title=title,
                    path=display_path(path, root=root),
                    summary=summary,
                    triggers=triggers,
                    entities=entities,
                    source="repo_script",
                    search_text=text[:20_000],
                )
            )
    return sorted(artifacts, key=lambda item: item.id)


def doc_type_for_path(root: Path, path: Path, settings: IndexSettings | None = None) -> str:
    settings = settings or IndexSettings()
    rel = path.relative_to(root)
    first = rel.parts[0] if rel.parts else ""
    if first in settings.memory_dirs:
        return "memory_doc"
    if first in settings.runbook_dirs or rel.name.startswith("app_"):
        return "runbook"
    if any(part in settings.custom_skill_dirs for part in rel.parts):
        return "skill_support_doc"
    return "doc"


def scan_markdown_docs(root: Path, settings: IndexSettings | None = None) -> list[Artifact]:
    settings = settings or IndexSettings()
    artifacts: list[Artifact] = []
    for path in sorted(root.rglob("*.md")):
        if should_skip_path(path):
            continue
        rel = path.relative_to(root)
        if rel.name == "SKILL.md":
            continue
        if rel.parts[0] == "knowledge" and rel.name != "README.md":
            continue
        text = safe_read_text(path)
        summary = first_heading_or_paragraph(text) or f"Markdown document {rel.as_posix()}"
        title = rel.with_suffix("").as_posix()
        artifact_type = doc_type_for_path(root, path, settings)
        artifact_id = f"{artifact_type}:{slugify(rel.with_suffix('').as_posix())}"
        triggers = significant_words(title, summary, " ".join(rel.parts), text[:4_000])
        entities = extract_entities(title, summary, text[:20_000], path.as_posix(), known_entities=settings.known_entities)
        artifacts.append(
            Artifact(
                id=artifact_id,
                type=artifact_type,
                title=title,
                path=display_path(path, root=root),
                summary=summary,
                triggers=triggers,
                entities=entities,
                source="repo_markdown",
                search_text=text[:20_000],
            )
        )
    return sorted(artifacts, key=lambda item: item.id)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def scan_cron_jobs(root: Path, hermes_home: Path, settings: IndexSettings | None = None) -> list[Artifact]:
    settings = settings or IndexSettings()
    jobs_path = hermes_home / "cron" / "jobs.json"
    payload = load_json(jobs_path)
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        return []

    artifacts: list[Artifact] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "")
        name = str(job.get("name") or job_id or "unnamed-cron")
        prompt = str(job.get("prompt") or "")
        schedule = str(job.get("schedule_display") or job.get("schedule") or "")
        script = str(job.get("script") or "")
        skills = [str(item) for item in job.get("skills") or []]
        enabled_toolsets = [str(item) for item in job.get("enabled_toolsets") or []]
        summary = first_sentence(prompt) or f"Cron job {name}"
        related = [f"skill:{slugify(skill)}" for skill in skills]
        if script:
            related.append(script)
        related.extend(extract_paths(prompt))
        artifact_id = f"cron:{slugify(name or job_id)}"
        triggers = significant_words(name, summary, schedule, script, " ".join(skills), " ".join(enabled_toolsets), prompt[:4_000])
        entities = extract_entities(name, summary, prompt[:20_000], script, known_entities=settings.known_entities)
        artifacts.append(
            Artifact(
                id=artifact_id,
                type="cron_job",
                title=name,
                path=f"{display_path(jobs_path)}#{job_id or slugify(name)}",
                summary=f"{summary} Schedule: {schedule}. State: {job.get('state') or 'unknown'}. Last status: {job.get('last_status') or 'unknown'}."[:700],
                triggers=triggers,
                entities=entities,
                related=unique_preserve_order(related),
                updated_at=str(job.get("updated_at") or job.get("created_at") or "") or None,
                source="hermes_cron_registry",
                search_text="\n".join([prompt, schedule, script, " ".join(skills), " ".join(enabled_toolsets)]),
            )
        )
    return sorted(artifacts, key=lambda item: item.id)


def first_sentence(text: str) -> str:
    clean = re.sub(r"\s+", " ", text.strip())
    if not clean:
        return ""
    match = re.search(r"(.{20,}?[.!?])\s", clean)
    if match:
        return match.group(1)[:500]
    return clean[:500]


def extract_paths(text: str) -> list[str]:
    paths = re.findall(r"(?:/home/[A-Za-z0-9_./-]+|~/[A-Za-z0-9_./-]+)", text)
    return unique_preserve_order(path.rstrip("`.,);]") for path in paths)


def load_yaml_if_available(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):  # type: ignore[name-defined]
        return None


def parse_mcp_servers_fallback(text: str) -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    in_mcp = False
    in_servers = False
    current: str | None = None
    for raw_line in text.splitlines():
        if re.match(r"^\S", raw_line):
            in_mcp = raw_line.startswith("mcp:")
            in_servers = False
            current = None
            continue
        if not in_mcp:
            continue
        if re.match(r"^\s+servers:\s*$", raw_line):
            in_servers = True
            current = None
            continue
        if not in_servers:
            continue
        server_match = re.match(r"^\s{4}([A-Za-z0-9_-]+):\s*$", raw_line)
        if server_match:
            server_name = server_match.group(1)
            current = server_name
            servers[server_name] = {}
            continue
        value_match = re.match(r"^\s{6}([A-Za-z0-9_-]+):\s*(.+)$", raw_line)
        if current and value_match:
            servers[current][value_match.group(1)] = value_match.group(2).strip().strip("'\"")
    return servers


def scan_mcp_servers(root: Path, hermes_home: Path, settings: IndexSettings | None = None) -> list[Artifact]:
    settings = settings or IndexSettings()
    config_path = hermes_home / "config.yaml"
    text = safe_read_text(config_path, max_chars=200_000)
    if not text:
        return []
    config = load_yaml_if_available(config_path)
    servers: dict[str, Any] = {}
    if isinstance(config, dict):
        mcp = config.get("mcp")
        if isinstance(mcp, dict):
            maybe_servers = mcp.get("servers")
            if isinstance(maybe_servers, dict):
                servers = maybe_servers
    if not servers:
        servers = parse_mcp_servers_fallback(text)

    artifacts: list[Artifact] = []
    for name, data in sorted(servers.items()):
        if not isinstance(data, dict):
            data = {}
        command = str(data.get("command") or "")
        url = str(data.get("url") or data.get("base_url") or "")
        args = data.get("args") or []
        env = data.get("env") or {}
        args_text = " ".join(str(item) for item in args) if isinstance(args, list) else str(args)
        env_text = " ".join(str(key) for key in env.keys()) if isinstance(env, dict) else ""
        summary_bits = [bit for bit in [f"command {command}" if command else "", f"url {url}" if url else "", args_text] if bit]
        summary = f"Hermes MCP server {name}: " + ("; ".join(summary_bits) if summary_bits else "configured in Hermes config")
        related = extract_paths(" ".join([command, args_text, json.dumps(env, sort_keys=True, default=str)]))
        artifact_id = f"mcp:{slugify(name)}"
        triggers = significant_words(name, summary, command, url, args_text, env_text)
        entities = extract_entities(name, summary, command, url, args_text, env_text, known_entities=settings.known_entities)
        artifacts.append(
            Artifact(
                id=artifact_id,
                type="mcp_server",
                title=name,
                path=f"{display_path(config_path)}#mcp.servers.{name}",
                summary=summary[:700],
                triggers=triggers,
                entities=entities,
                related=related,
                source="hermes_config",
                search_text="\n".join([summary, command, url, args_text, env_text]),
            )
        )
    return artifacts


def collect_artifacts(root: Path, hermes_home: Path, settings: IndexSettings | None = None) -> list[Artifact]:
    settings = settings or IndexSettings()
    root = root.expanduser().resolve()
    hermes_home = hermes_home.expanduser().resolve()
    artifacts = [
        *scan_skills(root, hermes_home, settings),
        *scan_scripts(root, settings),
        *scan_markdown_docs(root, settings),
        *scan_cron_jobs(root, hermes_home, settings),
        *scan_mcp_servers(root, hermes_home, settings),
    ]
    deduped: dict[str, Artifact] = {}
    for artifact in artifacts:
        deduped[artifact.id] = artifact
    return sorted(deduped.values(), key=lambda item: item.id)


def build_edges(artifacts: Sequence[Artifact]) -> list[Edge]:
    by_id = {artifact.id: artifact for artifact in artifacts}
    by_basename: dict[str, list[str]] = {}
    by_display_path: dict[str, str] = {}
    for artifact in artifacts:
        path = artifact.path.split("#", 1)[0]
        by_display_path[path] = artifact.id
        by_basename.setdefault(Path(path).name, []).append(artifact.id)

    edges: list[Edge] = []
    for artifact in artifacts:
        for related in artifact.related:
            target = resolve_related(related, by_id, by_basename, by_display_path)
            if target and target != artifact.id:
                edges.append(Edge(artifact.id, target, "related_to", related))

        if artifact.type == "skill":
            skill_words = set(significant_words(artifact.title, artifact.summary, " ".join(artifact.triggers), limit=20))
            for other in artifacts:
                if other.id == artifact.id or other.type not in {"script", "runbook", "memory_doc"}:
                    continue
                other_words = set(significant_words(other.title, other.summary, " ".join(other.triggers), limit=20))
                overlap = sorted((skill_words & other_words) - STOPWORDS)
                if len(overlap) >= 2:
                    edges.append(Edge(artifact.id, other.id, "keyword_overlap", ",".join(overlap[:5])))
    return dedupe_edges(edges)


def resolve_related(
    related: str,
    by_id: dict[str, Artifact],
    by_basename: dict[str, list[str]],
    by_display_path: dict[str, str],
) -> str | None:
    clean = related.strip()
    if clean in by_id:
        return clean
    if clean.startswith("skill:") and clean in by_id:
        return clean
    if ":" not in clean:
        skill_id = f"skill:{slugify(clean)}"
        if skill_id in by_id:
            return skill_id
    normalized = clean.replace(str(Path.home()), "~")
    if normalized in by_display_path:
        return by_display_path[normalized]
    basename = Path(clean).name
    basename = basename.rstrip("`.,);]")
    candidates = by_basename.get(basename, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def dedupe_edges(edges: Iterable[Edge]) -> list[Edge]:
    seen: set[tuple[str, str, str]] = set()
    output: list[Edge] = []
    for edge in edges:
        key = (edge.source, edge.target, edge.kind)
        if key in seen:
            continue
        seen.add(key)
        output.append(edge)
    return sorted(output, key=lambda item: (item.source, item.kind, item.target))


def write_jsonl(path: Path, artifacts: Sequence[Artifact]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for artifact in artifacts:
            row = asdict(artifact)
            row.pop("search_text", None)
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build_sqlite(path: Path, artifacts: Sequence[Artifact], edges: Sequence[Edge]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(
            """
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                path TEXT NOT NULL,
                summary TEXT NOT NULL,
                triggers_json TEXT NOT NULL,
                entities_json TEXT NOT NULL,
                related_json TEXT NOT NULL,
                updated_at TEXT,
                source TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE artifact_fts USING fts5(
                id UNINDEXED,
                type,
                title,
                summary,
                triggers,
                entities,
                path,
                search_text
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE edges (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                evidence TEXT NOT NULL,
                PRIMARY KEY (source, target, kind)
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO artifacts (
                id, type, title, path, summary, triggers_json, entities_json,
                related_json, updated_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    artifact.id,
                    artifact.type,
                    artifact.title,
                    artifact.path,
                    artifact.summary,
                    json.dumps(artifact.triggers, ensure_ascii=False),
                    json.dumps(artifact.entities, ensure_ascii=False),
                    json.dumps(artifact.related, ensure_ascii=False),
                    artifact.updated_at,
                    artifact.source,
                )
                for artifact in artifacts
            ],
        )
        conn.executemany(
            """
            INSERT INTO artifact_fts (id, type, title, summary, triggers, entities, path, search_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    artifact.id,
                    artifact.type,
                    artifact.title,
                    artifact.summary,
                    " ".join(artifact.triggers),
                    " ".join(artifact.entities),
                    artifact.path,
                    artifact.search_text,
                )
                for artifact in artifacts
            ],
        )
        conn.executemany(
            "INSERT INTO edges (source, target, kind, evidence) VALUES (?, ?, ?, ?)",
            [(edge.source, edge.target, edge.kind, edge.evidence) for edge in edges],
        )
        conn.commit()
    finally:
        conn.close()


def build_index(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
) -> tuple[list[Artifact], list[Edge]]:
    artifacts = collect_artifacts(root, hermes_home, settings)
    edges = build_edges(artifacts)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "index.jsonl", artifacts)
    build_sqlite(output_dir / "index.sqlite", artifacts, edges)
    return artifacts, edges


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_query_term(term: str) -> str:
    term = term.lower().strip()
    if len(term) > 4 and term.endswith("ies"):
        return f"{term[:-3]}y"
    if len(term) > 4 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def query_terms(query: str, *, drop_stopwords: bool = True) -> list[str]:
    terms: list[str] = []
    for raw_term in re.findall(r"[A-Za-z0-9]{2,}", query.lower()):
        term = normalize_query_term(raw_term)
        if drop_stopwords and term in QUERY_STOPWORDS:
            continue
        terms.append(term)
    return unique_preserve_order(terms)


def fts_query(query: str, *, operator: str = "AND") -> str:
    # FTS5 treats punctuation as syntax in bare MATCH terms (for example,
    # `manifest-backed*` can be parsed as `manifest` NOT column `backed`).
    # Split punctuation-heavy human queries into plain prefix terms instead.
    terms = query_terms(query)
    separator = " OR " if operator.upper() == "OR" else " "
    return separator.join(f"{term}*" for term in terms)


def token_hits(tokens: set[str], terms: Sequence[str]) -> int:
    hits = 0
    for term in terms:
        if any(token == term or token.startswith(term) for token in tokens):
            hits += 1
    return hits


def type_priority(artifact_type: str) -> int:
    return {
        "skill": 0,
        "script": 1,
        "cron_job": 2,
        "mcp_server": 3,
        "memory_doc": 4,
        "runbook": 5,
    }.get(artifact_type, 6)


def search_sort_key(row: dict[str, Any], terms: Sequence[str]) -> tuple[Any, ...]:
    title_source = " ".join([str(row.get("id") or ""), str(row.get("title") or "")])
    title_tokens = set(query_terms(title_source, drop_stopwords=False))
    path_tokens = set(query_terms(str(row.get("path") or ""), drop_stopwords=False))
    trigger_source = " ".join(row.get("triggers") or [])
    trigger_tokens = set(query_terms(trigger_source, drop_stopwords=False))
    summary_tokens = set(query_terms(str(row.get("summary") or ""), drop_stopwords=False))
    title_hits = token_hits(title_tokens, terms)
    path_hits = token_hits(path_tokens, terms)
    trigger_hits = token_hits(trigger_tokens, terms)
    summary_hits = token_hits(summary_tokens, terms)
    full_title_match = 0 if terms and title_hits == len(terms) else 1
    return (
        full_title_match,
        -title_hits,
        -path_hits,
        -trigger_hits,
        -summary_hits,
        type_priority(str(row.get("type") or "")),
        float(row.get("rank") or 0.0),
        str(row.get("title") or ""),
    )


def decode_artifact_row(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    output.pop("type_priority", None)
    for field_name in ("triggers_json", "entities_json", "related_json"):
        new_name = field_name.removesuffix("_json")
        try:
            output[new_name] = json.loads(output.pop(field_name))
        except (KeyError, TypeError, json.JSONDecodeError):
            output[new_name] = []
    return output


def search_index(db_path: Path, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    terms = query_terms(query)
    match = fts_query(query)
    if not match:
        return []
    conn = connect_readonly(db_path)
    try:
        candidate_limit = max(int(limit) * 10, 50)
        rows = conn.execute(
            """
            SELECT a.*, bm25(artifact_fts) AS rank,
                   CASE a.type
                     WHEN 'skill' THEN 0
                     WHEN 'script' THEN 1
                     WHEN 'cron_job' THEN 2
                     WHEN 'mcp_server' THEN 3
                     WHEN 'memory_doc' THEN 4
                     WHEN 'runbook' THEN 5
                     ELSE 6
                   END AS type_priority
            FROM artifact_fts
            JOIN artifacts a ON a.id = artifact_fts.id
            WHERE artifact_fts MATCH ?
            ORDER BY type_priority, rank, a.title
            LIMIT ?
            """,
            (match, candidate_limit),
        ).fetchall()
        if not rows and len(terms) > 1:
            rows = conn.execute(
                """
                SELECT a.*, bm25(artifact_fts) AS rank,
                       CASE a.type
                         WHEN 'skill' THEN 0
                         WHEN 'script' THEN 1
                         WHEN 'cron_job' THEN 2
                         WHEN 'mcp_server' THEN 3
                         WHEN 'memory_doc' THEN 4
                         WHEN 'runbook' THEN 5
                         ELSE 6
                       END AS type_priority
                FROM artifact_fts
                JOIN artifacts a ON a.id = artifact_fts.id
                WHERE artifact_fts MATCH ?
                ORDER BY type_priority, rank, a.title
                LIMIT ?
                """,
                (fts_query(query, operator="OR"), candidate_limit),
            ).fetchall()
        decoded = [decode_artifact_row(row) for row in rows]
        decoded.sort(key=lambda row: search_sort_key(row, terms))
        return decoded[: int(limit)]
    finally:
        conn.close()


def get_artifact(db_path: Path, artifact_id: str) -> dict[str, Any] | None:
    conn = connect_readonly(db_path)
    try:
        row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return decode_artifact_row(row) if row else None
    finally:
        conn.close()


def get_neighbors(db_path: Path, artifact_id: str) -> list[dict[str, Any]]:
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute(
            """
            SELECT e.kind, e.evidence, a.*
            FROM edges e
            JOIN artifacts a ON a.id = e.target
            WHERE e.source = ?
            UNION ALL
            SELECT e.kind, e.evidence, a.*
            FROM edges e
            JOIN artifacts a ON a.id = e.source
            WHERE e.target = ?
            ORDER BY kind, title
            """,
            (artifact_id, artifact_id),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = decode_artifact_row(row)
            item["edge_kind"] = item.pop("kind")
            item["edge_evidence"] = item.pop("evidence")
            output.append(item)
        return output
    finally:
        conn.close()


def print_results(rows: Sequence[dict[str, Any]]) -> None:
    for row in rows:
        print(f"{row['id']} [{row['type']}] {row['title']}")
        print(f"  path: {row['path']}")
        print(f"  summary: {row['summary']}")
        if row.get("edge_kind"):
            print(f"  edge: {row['edge_kind']} ({row.get('edge_evidence', '')})")
        if row.get("triggers"):
            print(f"  triggers: {', '.join(row['triggers'][:12])}")
        print()


def add_common_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=DEFAULT_OUTPUT_DIR / "index.sqlite", help="SQLite index path")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="build index.sqlite and index.jsonl")
    build_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="source directory to index")
    build_parser.add_argument("--hermes-home", type=Path, default=hermes_home_from_env(), help="Hermes home directory")
    build_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory")

    search_parser = subparsers.add_parser("search", help="search artifacts")
    search_parser.add_argument("query", help="search query")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--json", action="store_true", help="emit JSON")
    add_common_db_arg(search_parser)

    get_parser = subparsers.add_parser("get", help="show one artifact by id")
    get_parser.add_argument("artifact_id")
    get_parser.add_argument("--json", action="store_true", help="emit JSON")
    add_common_db_arg(get_parser)

    neighbors_parser = subparsers.add_parser("neighbors", help="show graph neighbors for one artifact")
    neighbors_parser.add_argument("artifact_id")
    neighbors_parser.add_argument("--json", action="store_true", help="emit JSON")
    add_common_db_arg(neighbors_parser)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "build":
        artifacts, edges = build_index(args.root, args.output_dir, args.hermes_home)
        counts: dict[str, int] = {}
        for artifact in artifacts:
            counts[artifact.type] = counts.get(artifact.type, 0) + 1
        print(f"Built {len(artifacts)} artifacts and {len(edges)} edges")
        for artifact_type, count in sorted(counts.items()):
            print(f"  {artifact_type}: {count}")
        print(f"SQLite: {args.output_dir / 'index.sqlite'}")
        print(f"JSONL:  {args.output_dir / 'index.jsonl'}")
        return 0

    if args.command == "search":
        rows = search_index(args.db, args.query, limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_results(rows)
        return 0

    if args.command == "get":
        row = get_artifact(args.db, args.artifact_id)
        if row is None:
            print(f"Artifact not found: {args.artifact_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_results([row])
        return 0

    if args.command == "neighbors":
        rows = get_neighbors(args.db, args.artifact_id)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_results(rows)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
