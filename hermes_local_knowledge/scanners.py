"""Artifact scanners and relationship builders."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .constants import SCRIPT_SUFFIXES, STOPWORDS
from .models import Artifact, Edge, IndexSettings
from .paths import display_path, iter_files_followlinks, path_is_relative_to
from .text_utils import (
    extract_code_identifiers,
    extract_entities,
    extract_env_names,
    extract_paths,
    first_heading_or_paragraph,
    first_sentence,
    identifier_terms,
    parse_frontmatter,
    regex_list_after_key,
    relpath_matches_config_dir,
    safe_read_text,
    significant_words,
    slugify,
    unique_preserve_order,
)


def skill_support_file_names(skill_dir: Path, excluded_dir_names: Sequence[str] | None = None) -> list[str]:
    names: list[str] = []
    for subdir in ("references", "templates", "scripts", "assets"):
        root = skill_dir / subdir
        if not root.exists():
            continue
        for child in iter_files_followlinks(
            root,
            allowed_roots=(skill_dir,),
            followlinks=False,
            excluded_dir_names=excluded_dir_names,
        ) or []:
            names.append(child.relative_to(skill_dir).as_posix())
    return names[:50]

def scan_skills(root: Path, hermes_home: Path, settings: IndexSettings | None = None) -> list[Artifact]:
    settings = settings or IndexSettings()
    artifacts: dict[str, Artifact] = {}
    sources = [
        *((root / rel, "custom_skill_source", (root,)) for rel in settings.custom_skill_dirs),
        (hermes_home / "skills", "runtime_skill", (root, hermes_home)),
    ]
    for skill_root, source, allowed_roots in sources:
        for skill_md in iter_files_followlinks(skill_root, filename="SKILL.md", allowed_roots=allowed_roots, excluded_dir_names=settings.exclude_dir_names) or []:
            text = safe_read_text(skill_md)
            fm = parse_frontmatter(text)
            name = str(fm.get("name") or skill_md.parent.name).strip()
            description = str(fm.get("description") or first_heading_or_paragraph(text)).strip()
            tags = fm.get("tags") if isinstance(fm.get("tags"), list) else regex_list_after_key(text, "tags")
            related_names = regex_list_after_key(text, "related_skills")
            support_files = skill_support_file_names(skill_md.parent, settings.exclude_dir_names)
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

def scan_runtime_skill_support_docs(root: Path, hermes_home: Path, settings: IndexSettings | None = None) -> list[Artifact]:
    """Index Markdown support docs for runtime skills that are not from source_root.

    Custom skills under ``source_root`` are already covered by ``scan_markdown_docs``.
    Runtime/bundled skill references live under ``$HERMES_HOME/skills`` and often
    contain the exact operational phrase an agent searches for.
    """

    settings = settings or IndexSettings()
    skill_root = hermes_home / "skills"
    artifacts: list[Artifact] = []
    resolved_root = root.expanduser().resolve()
    for skill_md in iter_files_followlinks(
        skill_root,
        filename="SKILL.md",
        allowed_roots=(root, hermes_home),
        excluded_dir_names=settings.exclude_dir_names,
    ) or []:
        try:
            if path_is_relative_to(skill_md.resolve(strict=True), resolved_root):
                continue
        except OSError:
            continue
        text = safe_read_text(skill_md)
        fm = parse_frontmatter(text)
        skill_name = str(fm.get("name") or skill_md.parent.name).strip()
        skill_id = f"skill:{slugify(skill_name)}"
        for subdir in ("references", "templates", "scripts", "assets"):
            support_root = skill_md.parent / subdir
            for path in iter_files_followlinks(
                support_root,
                suffixes={".md"},
                allowed_roots=(skill_md.parent,),
                followlinks=False,
                excluded_dir_names=settings.exclude_dir_names,
            ) or []:
                doc_text = safe_read_text(path)
                rel = path.relative_to(skill_md.parent)
                title = f"runtime_skills/{skill_name}/{rel.with_suffix('').as_posix()}"
                summary = first_heading_or_paragraph(doc_text) or f"Runtime skill support document {rel.as_posix()}"
                artifact_id = f"skill_support_doc:{slugify(title)}"
                triggers = significant_words(skill_name, title, summary, " ".join(rel.parts), doc_text[:4_000])
                entities = extract_entities(
                    skill_name, title, summary, doc_text[:20_000], path.as_posix(), known_entities=settings.known_entities
                )
                artifacts.append(
                    Artifact(
                        id=artifact_id,
                        type="skill_support_doc",
                        title=title,
                        path=display_path(path),
                        summary=summary,
                        triggers=triggers,
                        entities=entities,
                        related=[skill_id],
                        source="runtime_skill_support_doc",
                        search_text=doc_text[:20_000],
                    )
                )
    return sorted(artifacts, key=lambda item: item.id)

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
        for path in iter_files_followlinks(script_root, suffixes=SCRIPT_SUFFIXES, allowed_roots=(root,), excluded_dir_names=settings.exclude_dir_names) or []:
            text = safe_read_text(path, max_chars=50_000)
            rel = path.relative_to(root)
            title = rel.as_posix()
            summary = script_summary(path, text)
            artifact_id = f"script:{slugify(rel.as_posix())}"
            env_names = extract_env_names(text)
            code_identifiers = extract_code_identifiers(text)
            metadata_terms = identifier_terms(
                title,
                summary,
                " ".join(rel.parts),
                " ".join(env_names),
                " ".join(code_identifiers),
                known_entities=settings.known_entities,
            )
            triggers = significant_words(title, summary, " ".join(rel.parts), " ".join(metadata_terms))
            entities = extract_entities(
                title,
                summary,
                path.as_posix(),
                " ".join(metadata_terms),
                known_entities=settings.known_entities,
            )
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
                    search_text="\n".join(
                        [
                            title,
                            summary,
                            " ".join(rel.parts),
                            " ".join(env_names),
                            " ".join(code_identifiers),
                            " ".join(metadata_terms),
                        ]
                    ),
                )
            )
    return sorted(artifacts, key=lambda item: item.id)

def doc_type_for_path(root: Path, path: Path, settings: IndexSettings | None = None) -> str:
    settings = settings or IndexSettings()
    rel = path.relative_to(root)
    if relpath_matches_config_dir(rel, settings.memory_dirs):
        return "memory_doc"
    if relpath_matches_config_dir(rel, settings.runbook_dirs) or rel.name.startswith("app_"):
        return "runbook"
    if relpath_matches_config_dir(rel, settings.custom_skill_dirs):
        return "skill_support_doc"
    return "doc"

def scan_markdown_docs(
    root: Path,
    settings: IndexSettings | None = None,
    *,
    excluded_roots: Sequence[Path] = (),
) -> list[Artifact]:
    settings = settings or IndexSettings()
    artifacts: list[Artifact] = []
    resolved_excluded_roots = tuple(path.expanduser().resolve() for path in excluded_roots if path.exists())
    for path in iter_files_followlinks(root, suffixes={".md"}, allowed_roots=(root,), followlinks=False, excluded_dir_names=settings.exclude_dir_names) or []:
        if any(path_is_relative_to(path.resolve(), excluded_root) for excluded_root in resolved_excluded_roots):
            continue
        rel = path.relative_to(root)
        if rel.name == "SKILL.md":
            continue
        text = safe_read_text(path)
        summary = first_heading_or_paragraph(text) or f"Markdown document {rel.as_posix()}"
        title = rel.with_suffix("").as_posix()
        artifact_type = doc_type_for_path(root, path, settings)
        artifact_id = f"{artifact_type}:{slugify(rel.with_suffix('').as_posix())}"
        related: list[str] = []
        if artifact_type == "skill_support_doc":
            for parent in path.parents:
                if parent == root:
                    break
                skill_md = parent / "SKILL.md"
                if skill_md.exists():
                    skill_text = safe_read_text(skill_md)
                    fm = parse_frontmatter(skill_text)
                    skill_name = str(fm.get("name") or parent.name).strip()
                    related = [f"skill:{slugify(skill_name)}"]
                    break
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
                related=related,
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
        metadata_terms = identifier_terms(
            name,
            schedule,
            script,
            " ".join(skills),
            " ".join(enabled_toolsets),
            prompt[:4_000],
            known_entities=settings.known_entities,
        )
        triggers = significant_words(
            name,
            summary,
            schedule,
            script,
            " ".join(skills),
            " ".join(enabled_toolsets),
            prompt[:4_000],
            " ".join(metadata_terms),
        )
        entities = extract_entities(
            name,
            summary,
            prompt[:20_000],
            script,
            " ".join(metadata_terms),
            known_entities=settings.known_entities,
        )
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
                search_text="\n".join([
                    prompt,
                    schedule,
                    script,
                    " ".join(skills),
                    " ".join(enabled_toolsets),
                    " ".join(metadata_terms),
                ]),
            )
        )
    return sorted(artifacts, key=lambda item: item.id)

def load_yaml_if_available(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):  # type: ignore[name-defined]
        return None

def parse_mcp_servers_fallback(text: str) -> dict[str, tuple[dict[str, Any], str]]:
    servers: dict[str, tuple[dict[str, Any], str]] = {}
    section: str | None = None
    in_servers = False
    current: str | None = None
    current_path = "mcp.servers"
    for raw_line in text.splitlines():
        top_level = re.match(r"^([A-Za-z0-9_-]+):\s*$", raw_line)
        if top_level:
            section = top_level.group(1)
            in_servers = section == "mcp_servers"
            current_path = "mcp_servers" if in_servers else "mcp.servers"
            current = None
            continue
        if section not in {"mcp", "mcp_servers"}:
            continue
        if section == "mcp" and re.match(r"^\s+servers:\s*$", raw_line):
            in_servers = True
            current_path = "mcp.servers"
            current = None
            continue
        if not in_servers:
            continue
        server_indent = 2 if section == "mcp_servers" else 4
        value_indent = server_indent + 2
        server_match = re.match(rf"^\s{{{server_indent}}}([A-Za-z0-9_-]+):\s*$", raw_line)
        if server_match:
            server_name = server_match.group(1)
            current = server_name
            servers[server_name] = ({}, current_path)
            continue
        value_match = re.match(rf"^\s{{{value_indent}}}([A-Za-z0-9_-]+):\s*(.+)$", raw_line)
        if current and value_match:
            servers[current][0][value_match.group(1)] = value_match.group(2).strip().strip("'\"")
    return servers

def scan_mcp_servers(root: Path, hermes_home: Path, settings: IndexSettings | None = None) -> list[Artifact]:
    settings = settings or IndexSettings()
    config_path = hermes_home / "config.yaml"
    text = safe_read_text(config_path, max_chars=200_000)
    if not text:
        return []
    config = load_yaml_if_available(config_path)
    servers: dict[str, tuple[Any, str]] = {}
    if isinstance(config, dict):
        mcp = config.get("mcp")
        if isinstance(mcp, dict):
            maybe_servers = mcp.get("servers")
            if isinstance(maybe_servers, dict):
                servers.update((str(name), (data, "mcp.servers")) for name, data in maybe_servers.items())
        native_servers = config.get("mcp_servers")
        if isinstance(native_servers, dict):
            servers.update((str(name), (data, "mcp_servers")) for name, data in native_servers.items())
    if not servers:
        servers = parse_mcp_servers_fallback(text)

    artifacts: list[Artifact] = []
    for name, (data, config_path_key) in sorted(servers.items()):
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
        related = extract_paths(" ".join([command, args_text, url]))
        artifact_id = f"mcp:{slugify(name)}"
        metadata_terms = identifier_terms(name, summary, command, url, args_text, env_text, known_entities=settings.known_entities)
        triggers = significant_words(name, summary, command, url, args_text, env_text, " ".join(metadata_terms))
        entities = extract_entities(
            name,
            summary,
            command,
            url,
            args_text,
            env_text,
            " ".join(metadata_terms),
            known_entities=settings.known_entities,
        )
        artifacts.append(
            Artifact(
                id=artifact_id,
                type="mcp_server",
                title=name,
                path=f"{display_path(config_path)}#{config_path_key}.{name}",
                summary=summary[:700],
                triggers=triggers,
                entities=entities,
                related=related,
                source="hermes_config",
                search_text="\n".join([summary, command, url, args_text, env_text, " ".join(metadata_terms)]),
            )
        )
    return artifacts


def _frontmatter_list(fm: dict[str, Any], key: str) -> list[str]:
    value = fm.get(key)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def scan_tool_okfs(
    okf_root: Path | None,
    root: Path,
    settings: IndexSettings | None = None,
) -> list[Artifact]:
    """Index generated, state-local tool OKF Markdown files."""

    if okf_root is None or not okf_root.exists():
        return []
    settings = settings or IndexSettings()
    artifacts: list[Artifact] = []
    for path in iter_files_followlinks(
        okf_root,
        suffixes={".md"},
        allowed_roots=(okf_root,),
        followlinks=False,
        excluded_dir_names=settings.exclude_dir_names,
    ) or []:
        text = safe_read_text(path)
        fm = parse_frontmatter(text)
        artifact_type = str(fm.get("artifact_type") or "tool_okf").strip()
        if artifact_type != "tool_okf":
            continue
        tool = str(fm.get("tool") or "").strip()
        schema_digest = str(fm.get("schema_hash") or "").strip()
        if not tool or not schema_digest:
            continue
        toolset = str(fm.get("toolset") or "").strip()
        aliases = _frontmatter_list(fm, "aliases")
        frontmatter_triggers = _frontmatter_list(fm, "triggers")
        when_not_to_use = _frontmatter_list(fm, "when_not_to_use")
        related_tools = _frontmatter_list(fm, "related_tools")
        title = str(fm.get("title") or f"Tool OKF: {tool}").strip()
        summary = first_heading_or_paragraph(text) or f"Generated OKF for Hermes tool {tool}"
        metadata_terms = identifier_terms(
            tool,
            toolset,
            title,
            " ".join(aliases),
            " ".join(frontmatter_triggers),
            known_entities=settings.known_entities,
        )
        triggers = significant_words(
            tool,
            toolset,
            title,
            summary,
            " ".join(aliases),
            " ".join(frontmatter_triggers),
            " ".join(when_not_to_use),
            " ".join(metadata_terms),
        )
        entities = extract_entities(
            tool,
            toolset,
            title,
            summary,
            " ".join(aliases),
            " ".join(frontmatter_triggers),
            text[:20_000],
            known_entities=settings.known_entities,
        )
        related = [f"tool_okf:{slugify(item)}" for item in related_tools]
        artifacts.append(
            Artifact(
                id=f"tool_okf:{slugify(tool)}",
                type="tool_okf",
                title=title,
                path=display_path(path, root=root),
                summary=summary,
                triggers=unique_preserve_order([*aliases, *frontmatter_triggers, *triggers]),
                entities=entities,
                related=unique_preserve_order(related),
                updated_at=str(fm.get("generated_at") or "") or None,
                source="generated_tool_okf",
                search_text="\n".join(
                    [
                        tool,
                        toolset,
                        title,
                        summary,
                        " ".join(aliases),
                        " ".join(frontmatter_triggers),
                        " ".join(when_not_to_use),
                        " ".join(related_tools),
                        " ".join(metadata_terms),
                        text[:20_000],
                    ]
                ),
            )
        )
    return sorted(artifacts, key=lambda item: item.id)


def collect_artifacts(
    root: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
    *,
    okf_root: Path | None = None,
) -> list[Artifact]:
    settings = settings or IndexSettings()
    root = root.expanduser().resolve()
    hermes_home = hermes_home.expanduser().resolve()
    artifacts = [
        *scan_skills(root, hermes_home, settings),
        *scan_runtime_skill_support_docs(root, hermes_home, settings),
        *scan_scripts(root, settings),
        *(scan_markdown_docs(root, settings, excluded_roots=(okf_root,) if okf_root else ()) if settings.include_markdown_docs else []),
        *scan_cron_jobs(root, hermes_home, settings),
        *scan_mcp_servers(root, hermes_home, settings),
        *scan_tool_okfs(okf_root, root, settings),
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
