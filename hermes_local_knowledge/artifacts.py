"""Whole-artifact models, scanners, collection, and graph edges.

This is the side-by-side rewrite core.  It deliberately depends only on the
standard library and on the small settings shape defined by ``ScannerSettings``.
The configuration module owns how those settings are resolved.
"""

from __future__ import annotations

import getpass
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator, Protocol, Sequence

_SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".bash", ".cjs", ".mjs", ".js"})
_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".archive",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".venv",
        ".worktrees",
        "__pycache__",
        "htmlcov",
        "logs",
        "node_modules",
        "venv",
        "worktrees",
    }
)


def _runtime_stopwords() -> set[str]:
    try:
        username = getpass.getuser().strip().lower()
    except Exception:
        return set()
    return {username} if len(username) >= 3 else set()


_STOPWORDS = frozenset(
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

_MCP_URI_AUTHORITY_RE = re.compile(r"(?i)(?P<prefix>[a-z][a-z0-9+.-]*://)(?P<authority>[^/?#\s]*)")
_HTTP_URL_SPAN_RE = re.compile(r'https?://[^\s`"<>]+', re.IGNORECASE)
_LOCAL_PATH_RE = re.compile(
    r"""
    (?P<home>(?<![\w~])~/[^\s`"'<>|?*]+)
    |(?P<drive>(?<!\w)[A-Za-z]:[\\/][^\s`"'<>|?*]+)
    |(?P<unc>(?<!\\)\\\\[^\\/\s`"'<>|?*]+\\[^\\/\s`"'<>|?*]+(?:\\[^\\/\s`"'<>|?*]+)*)
    |(?P<posix>(?<![\w:/~.-])/(?!/)[^\s`"'<>|?*]+)
    """,
    re.VERBOSE,
)


class ScannerSettings(Protocol):
    """The scanner-facing subset of the configuration model."""

    @property
    def custom_skill_dirs(self) -> Sequence[str]: ...

    @property
    def script_dirs(self) -> Sequence[str]: ...

    @property
    def memory_dirs(self) -> Sequence[str]: ...

    @property
    def runbook_dirs(self) -> Sequence[str]: ...

    @property
    def known_entities(self) -> Sequence[str]: ...

    @property
    def include_markdown_docs(self) -> bool: ...

    @property
    def exclude_dir_names(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class Artifact:
    """One whole artifact exposed to indexing and model-facing lookup results."""

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


_SourceIdentity = tuple[str | int, ...]


@dataclass(frozen=True)
class _Candidate:
    artifact: Artifact
    source_identity: _SourceIdentity
    priority: int = 0


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "artifact"


def _unique(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = str(value).strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        output.append(clean)
    return output


def _portable_basename(value: str) -> str:
    clean = str(value).rstrip("`.,);]")
    path_type = PureWindowsPath if "\\" in clean else PurePosixPath
    return path_type(clean).name


def _display_path(path: Path, *, root: Path | None = None) -> str:
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


def _safe_read_text(path: Path, *, max_chars: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(max_chars)
    except OSError:
        return ""


def _parse_bracket_list(value: str) -> list[str]:
    clean = value.strip()
    if clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1]
    return [item.strip().strip("'\"") for item in clean.split(",") if item.strip().strip("'\"")]


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    frontmatter: dict[str, Any] = {}
    current_key: str | None = None
    for line in text.splitlines()[1:]:
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
            frontmatter[key] = _parse_bracket_list(value)
        else:
            frontmatter[key] = value.strip("'\"")
    return frontmatter


def _frontmatter_list(frontmatter: dict[str, Any], key: str) -> list[str]:
    value = frontmatter.get(key)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _regex_list_after_key(text: str, key: str) -> list[str]:
    inline = re.search(rf"^\s*{re.escape(key)}:\s*\[([^\]]*)\]", text, re.MULTILINE)
    if inline:
        return _parse_bracket_list(inline.group(1))
    block = re.search(rf"^\s*{re.escape(key)}:\s*\n((?:\s+-\s+.+\n?)+)", text, re.MULTILINE)
    if not block:
        return []
    return [line.split("-", 1)[1].strip().strip("'\"") for line in block.group(1).splitlines() if "-" in line]


def _first_heading_or_paragraph(text: str) -> str:
    lines = text.splitlines()
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if in_frontmatter:
            if index > 0 and line == "---":
                in_frontmatter = False
            continue
        if not line or line.startswith("---") or line.startswith("```"):
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        return re.sub(r"\s+", " ", line)[:500]
    return ""


def _first_sentence(text: str) -> str:
    clean = re.sub(r"\s+", " ", text.strip())
    if not clean:
        return ""
    match = re.search(r"(.{20,}?[.!?])\s", clean)
    return (match.group(1) if match else clean)[:500]


def _significant_words(*parts: str, limit: int = 30) -> list[str]:
    words: list[str] = []
    for part in parts:
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{2,}", part):
            lowered = word.strip("._-").lower()
            if len(lowered) >= 3 and lowered not in _STOPWORDS:
                words.append(lowered)
    return _unique(words)[:limit]


def _split_identifier(value: str) -> list[str]:
    pieces: list[str] = []
    for segment in re.split(r"[^A-Za-z0-9]+", value):
        if not segment:
            continue
        spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", segment)
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", spaced)
        pieces.extend(part.lower() for part in spaced.split() if part)
    return pieces


def _known_entity_aliases(known_entities: Sequence[str]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for entity in known_entities:
        words = _split_identifier(entity)
        if not words:
            continue
        expansions = _unique([*words, "".join(words), entity.lower()])
        keys = {"".join(words), entity.lower().replace(" ", "")}
        if len(words) == 1:
            keys.add(words[0])
        elif len(words) > 1:
            acronym = "".join(word[0] for word in words)
            if len(acronym) >= 2:
                keys.add(acronym)
        for key in filter(None, keys):
            aliases.setdefault(key, []).extend(expansions)
    return {key: _unique(values) for key, values in aliases.items()}


def _identifier_terms(*parts: str, known_entities: Sequence[str], limit: int = 80) -> list[str]:
    aliases = _known_entity_aliases(known_entities)
    terms: list[str] = []
    for part in parts:
        for raw in re.findall(r"[A-Za-z][A-Za-z0-9_./:+-]*", part):
            for token in _split_identifier(raw):
                if not token:
                    continue
                terms.append(token)
                terms.extend(aliases.get(token, []))
    return _unique(term for term in terms if term and term not in _STOPWORDS)[:limit]


def _extract_env_names(text: str, *, limit: int = 80) -> list[str]:
    names: list[str] = []
    env_name = r"[A-Z_][A-Z0-9_]{2,}"
    string_name = r"[A-Za-z_][A-Za-z0-9_]*"
    patterns = (
        rf"(?:^|[\s;&])(?:export\s+)?({env_name})\s*=",
        rf"\$\{{?({env_name})",
        rf"os\.environ\s*\[\s*[\"']({string_name})[\"']\s*\]",
        rf"os\.(?:environ(?:\.get)?|getenv)\s*\(\s*[\"']({string_name})[\"']",
        rf"process\.env\.({string_name})",
        rf"process\.env\s*\[\s*[\"']({string_name})[\"']\s*\]",
    )
    for pattern in patterns:
        names.extend(match.group(1) for match in re.finditer(pattern, text, re.MULTILINE))
    return _unique(names)[:limit]


def _extract_code_identifiers(text: str, *, limit: int = 80) -> list[str]:
    names: list[str] = []
    patterns = (
        r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*import\s+([A-Za-z_][A-Za-z0-9_.]*)",
        r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+([A-Za-z_][A-Za-z0-9_.*]*)",
        r"--([A-Za-z][A-Za-z0-9_-]{2,})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.MULTILINE):
            names.extend(group for group in match.groups() if group)
    return _unique(names)[:limit]


def _extract_entities(*parts: str, known_entities: Sequence[str]) -> list[str]:
    haystack = "\n".join(parts).lower()
    return _unique(entity for entity in known_entities if entity.lower() in haystack)


def _extract_paths(text: str) -> list[str]:
    text_without_urls = _HTTP_URL_SPAN_RE.sub(" ", text)
    paths = (match.group(0) for match in _LOCAL_PATH_RE.finditer(text_without_urls))
    return _unique(path.rstrip("`.,);]:") for path in paths)


def _relative_config_parts(value: str) -> tuple[str, ...]:
    return tuple(part for part in Path(str(value)).parts if part not in ("", "."))


def _relpath_matches_config_dir(relative_path: Path, configured_dirs: Sequence[str]) -> bool:
    for configured in configured_dirs:
        parts = _relative_config_parts(configured)
        if parts and relative_path.parts[: len(parts)] == parts:
            return True
    return False


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolved_allowed_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    output: list[Path] = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved not in output:
            output.append(resolved)
    return tuple(output)


def _relative_to_most_specific_root(path: Path, allowed_roots: Sequence[Path]) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    matching = [root for root in allowed_roots if _path_is_relative_to(resolved, root)]
    if not matching:
        return None
    return resolved.relative_to(max(matching, key=lambda root: len(root.parts)))


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        result = path.stat()
    except OSError:
        return None
    return result.st_dev, result.st_ino


def _source_identity(path: Path) -> _SourceIdentity:
    key = _stat_key(path)
    if key is not None:
        return ("inode", *key)
    try:
        return ("path", path.resolve().as_posix())
    except (OSError, RuntimeError):
        return ("path", path.absolute().as_posix())


def _iter_files(
    root: Path,
    *,
    filename: str | None = None,
    suffixes: set[str] | frozenset[str] | None = None,
    allowed_roots: Sequence[Path],
    followlinks: bool = True,
    excluded_dir_names: Sequence[str] = (),
) -> Iterator[Path]:
    root = root.expanduser()
    if not root.exists():
        return
    allowed = _resolved_allowed_roots(allowed_roots)
    if not allowed:
        return
    excluded = _EXCLUDED_DIR_NAMES | frozenset(excluded_dir_names)
    seen_dirs: set[tuple[int, int]] = set()
    seen_files: set[tuple[int, int]] = set()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=followlinks):
        current = Path(dirpath)
        relative = _relative_to_most_specific_root(current, allowed)
        current_key = _stat_key(current)
        if relative is None or any(part in excluded for part in relative.parts):
            dirnames[:] = []
            continue
        if current_key is None or current_key in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(current_key)

        kept: list[str] = []
        pending_keys: set[tuple[int, int]] = set()
        for dirname in sorted(dirnames, key=lambda name: ((current / name).is_symlink(), name)):
            child = current / dirname
            if dirname in excluded or (child.is_symlink() and not followlinks):
                continue
            child_relative = _relative_to_most_specific_root(child, allowed)
            child_key = _stat_key(child)
            if child_relative is None or any(part in excluded for part in child_relative.parts):
                continue
            if child_key is None or child_key in seen_dirs or child_key in pending_keys:
                continue
            pending_keys.add(child_key)
            kept.append(dirname)
        dirnames[:] = kept

        for file_name in sorted(filenames):
            path = current / file_name
            relative = _relative_to_most_specific_root(path, allowed)
            if relative is None or any(part in excluded for part in relative.parts):
                continue
            if filename is not None and file_name != filename:
                continue
            if suffixes is not None and path.suffix not in suffixes:
                continue
            file_key = _stat_key(path)
            if file_key is None or file_key in seen_files:
                continue
            seen_files.add(file_key)
            yield path


def _candidate(artifact: Artifact, path: Path, *, priority: int = 0) -> _Candidate:
    return _Candidate(artifact, _source_identity(path), priority)


def _dedupe_candidates(candidates: Iterable[_Candidate]) -> list[Artifact]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.priority,
            item.artifact.source or "",
            item.artifact.path,
            item.artifact.id,
        ),
    )
    seen_ids: set[str] = set()
    seen_sources: set[_SourceIdentity] = set()
    output: list[Artifact] = []
    for candidate in ordered:
        if candidate.artifact.id in seen_ids or candidate.source_identity in seen_sources:
            continue
        seen_ids.add(candidate.artifact.id)
        seen_sources.add(candidate.source_identity)
        output.append(candidate.artifact)
    return sorted(output, key=lambda artifact: artifact.id)


def _skill_support_file_names(skill_dir: Path, excluded_dir_names: Sequence[str]) -> list[str]:
    names: list[str] = []
    for subdir in ("references", "templates", "scripts", "assets"):
        support_root = skill_dir / subdir
        for child in _iter_files(
            support_root,
            allowed_roots=(skill_dir,),
            followlinks=False,
            excluded_dir_names=excluded_dir_names,
        ):
            names.append(child.relative_to(skill_dir).as_posix())
    return names[:50]


def _skill_candidates(root: Path, hermes_home: Path, settings: ScannerSettings) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    sources = [
        *((root / relative, "custom_skill_source", (root,), 0) for relative in settings.custom_skill_dirs),
        (hermes_home / "skills", "runtime_skill", (root, hermes_home), 10),
    ]
    for skill_root, source, allowed_roots, priority in sources:
        for skill_md in _iter_files(
            skill_root,
            filename="SKILL.md",
            allowed_roots=allowed_roots,
            excluded_dir_names=settings.exclude_dir_names,
        ):
            text = _safe_read_text(skill_md)
            frontmatter = _parse_frontmatter(text)
            name = str(frontmatter.get("name") or skill_md.parent.name).strip()
            description = str(frontmatter.get("description") or _first_heading_or_paragraph(text)).strip()
            tags_value = frontmatter.get("tags")
            tags = (
                [str(item) for item in tags_value]
                if isinstance(tags_value, list)
                else _regex_list_after_key(text, "tags")
            )
            related_names = _regex_list_after_key(text, "related_skills")
            support_files = _skill_support_file_names(skill_md.parent, settings.exclude_dir_names)
            try:
                category_parts = list(skill_md.parent.relative_to(skill_root).parts[:-1])
            except ValueError:
                category_parts = []
            metadata = " ".join([*tags, *category_parts, *support_files])
            artifact = Artifact(
                id=f"skill:{_slugify(name)}",
                type="skill",
                title=name,
                path=_display_path(skill_md.parent, root=root),
                summary=description,
                triggers=_significant_words(name, description, metadata),
                entities=_extract_entities(
                    name,
                    description,
                    " ".join(tags),
                    skill_md.as_posix(),
                    known_entities=settings.known_entities,
                ),
                related=_unique(f"skill:{_slugify(item)}" for item in related_names),
                source=source,
                search_text="\n".join([description, " ".join(tags), " ".join(support_files)]),
            )
            candidates.append(_candidate(artifact, skill_md, priority=priority))
    return candidates


def _parent_skill_id(path: Path, stop: Path) -> str | None:
    allowed = _resolved_allowed_roots((stop,))
    for parent in path.parents:
        if parent == stop:
            break
        skill_md = parent / "SKILL.md"
        if not skill_md.exists() or _relative_to_most_specific_root(skill_md, allowed) is None:
            continue
        text = _safe_read_text(skill_md)
        frontmatter = _parse_frontmatter(text)
        name = str(frontmatter.get("name") or parent.name).strip()
        return f"skill:{_slugify(name)}"
    return None


def _markdown_candidate(
    root: Path,
    path: Path,
    artifact_type: str,
    settings: ScannerSettings,
    *,
    source: str = "repo_markdown",
    title: str | None = None,
    related: Sequence[str] = (),
    relative_display_path: bool = True,
    fallback_summary: str | None = None,
    leading_metadata: Sequence[str] = (),
    priority: int = 0,
) -> _Candidate:
    text = _safe_read_text(path)
    relative = path.relative_to(root)
    artifact_title = title or relative.with_suffix("").as_posix()
    summary = _first_heading_or_paragraph(text) or fallback_summary or f"Markdown document {relative.as_posix()}"
    artifact = Artifact(
        id=f"{artifact_type}:{_slugify(artifact_title)}",
        type=artifact_type,
        title=artifact_title,
        path=_display_path(path, root=root if relative_display_path else None),
        summary=summary,
        triggers=_significant_words(
            *leading_metadata,
            artifact_title,
            summary,
            " ".join(relative.parts),
            text[:4_000],
        ),
        entities=_extract_entities(
            *leading_metadata,
            artifact_title,
            summary,
            text[:20_000],
            path.as_posix(),
            known_entities=settings.known_entities,
        ),
        related=list(related),
        source=source,
        search_text=text[:20_000],
    )
    return _candidate(artifact, path, priority=priority)


def _custom_support_candidates(root: Path, settings: ScannerSettings) -> list[_Candidate]:
    if not settings.include_markdown_docs:
        return []
    candidates: list[_Candidate] = []
    for relative_dir in settings.custom_skill_dirs:
        support_root = root / relative_dir
        for path in _iter_files(
            support_root,
            suffixes={".md"},
            allowed_roots=(root,),
            excluded_dir_names=settings.exclude_dir_names,
        ):
            if path.name == "SKILL.md":
                continue
            parent = _parent_skill_id(path, root)
            candidates.append(
                _markdown_candidate(
                    root,
                    path,
                    "skill_support_doc",
                    settings,
                    related=(parent,) if parent else (),
                )
            )
    return candidates


def _runtime_support_candidates(root: Path, hermes_home: Path, settings: ScannerSettings) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    skill_root = hermes_home / "skills"
    resolved_root = root.resolve()
    for skill_md in _iter_files(
        skill_root,
        filename="SKILL.md",
        allowed_roots=(root, hermes_home),
        excluded_dir_names=settings.exclude_dir_names,
    ):
        try:
            if _path_is_relative_to(skill_md.resolve(strict=True), resolved_root):
                continue
        except OSError:
            continue
        text = _safe_read_text(skill_md)
        frontmatter = _parse_frontmatter(text)
        skill_name = str(frontmatter.get("name") or skill_md.parent.name).strip()
        for subdir in ("references", "templates", "scripts", "assets"):
            support_root = skill_md.parent / subdir
            for path in _iter_files(
                support_root,
                suffixes={".md"},
                allowed_roots=(skill_md.parent,),
                followlinks=False,
                excluded_dir_names=settings.exclude_dir_names,
            ):
                relative = path.relative_to(skill_md.parent)
                title = f"runtime_skills/{skill_name}/{relative.with_suffix('').as_posix()}"
                candidates.append(
                    _markdown_candidate(
                        skill_md.parent,
                        path,
                        "skill_support_doc",
                        settings,
                        source="runtime_skill_support_doc",
                        title=title,
                        related=(f"skill:{_slugify(skill_name)}",),
                        relative_display_path=False,
                        fallback_summary=f"Runtime skill support document {relative.as_posix()}",
                        leading_metadata=(skill_name,),
                        priority=10,
                    )
                )
    return candidates


def scan_skills_and_support_docs(
    root: Path,
    hermes_home: Path,
    settings: ScannerSettings,
) -> list[Artifact]:
    """Scan configured/runtime skills and their Markdown support documents."""

    root = root.expanduser().resolve()
    hermes_home = hermes_home.expanduser().resolve()
    return _dedupe_candidates(
        [
            *_skill_candidates(root, hermes_home, settings),
            *_custom_support_candidates(root, settings),
            *_runtime_support_candidates(root, hermes_home, settings),
        ]
    )


def _script_summary(path: Path, text: str) -> str:
    docstring = re.search(r'^[ruRUfbFB]*(["\']{3})(.*?)\1', text, re.DOTALL | re.MULTILINE)
    if docstring:
        return re.sub(r"\s+", " ", docstring.group(2).strip())[:500]
    comments: list[str] = []
    for raw_line in text.splitlines()[:30]:
        line = raw_line.strip()
        if line.startswith("#!") or not line:
            continue
        if line.startswith("#"):
            comments.append(line.lstrip("#").strip())
            continue
        break
    if comments:
        return re.sub(r"\s+", " ", " ".join(comments))[:500]
    return f"Local script {path.name}"


def _script_candidates(root: Path, settings: ScannerSettings) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for relative_dir in settings.script_dirs:
        script_root = root / relative_dir
        for path in _iter_files(
            script_root,
            suffixes=_SCRIPT_SUFFIXES,
            allowed_roots=(root,),
            excluded_dir_names=settings.exclude_dir_names,
        ):
            text = _safe_read_text(path, max_chars=50_000)
            relative = path.relative_to(root)
            title = relative.as_posix()
            summary = _script_summary(path, text)
            env_names = _extract_env_names(text)
            code_identifiers = _extract_code_identifiers(text)
            metadata_terms = _identifier_terms(
                title,
                summary,
                " ".join(relative.parts),
                " ".join(env_names),
                " ".join(code_identifiers),
                known_entities=settings.known_entities,
            )
            artifact = Artifact(
                id=f"script:{_slugify(relative.as_posix())}",
                type="script",
                title=title,
                path=_display_path(path, root=root),
                summary=summary,
                triggers=_significant_words(title, summary, " ".join(relative.parts), " ".join(metadata_terms)),
                entities=_extract_entities(
                    title,
                    summary,
                    path.as_posix(),
                    " ".join(metadata_terms),
                    known_entities=settings.known_entities,
                ),
                source="repo_script",
                search_text="\n".join(
                    [
                        title,
                        summary,
                        " ".join(relative.parts),
                        " ".join(env_names),
                        " ".join(code_identifiers),
                        " ".join(metadata_terms),
                    ]
                ),
            )
            candidates.append(_candidate(artifact, path))
    return candidates


def _doc_type(relative: Path, settings: ScannerSettings) -> str:
    if _relpath_matches_config_dir(relative, settings.memory_dirs):
        return "memory_doc"
    if _relpath_matches_config_dir(relative, settings.runbook_dirs) or relative.name.startswith("app_"):
        return "runbook"
    return "doc"


def _source_markdown_candidates(
    root: Path,
    settings: ScannerSettings,
    excluded_roots: Sequence[Path],
) -> list[_Candidate]:
    if not settings.include_markdown_docs:
        return []
    resolved_excluded = tuple(path.expanduser().resolve() for path in excluded_roots if path.exists())
    candidates: list[_Candidate] = []
    for path in _iter_files(
        root,
        suffixes={".md"},
        allowed_roots=(root,),
        excluded_dir_names=settings.exclude_dir_names,
    ):
        resolved = path.resolve()
        if any(_path_is_relative_to(resolved, excluded) for excluded in resolved_excluded):
            continue
        relative = path.relative_to(root)
        if relative.name == "SKILL.md" or _relpath_matches_config_dir(relative, settings.custom_skill_dirs):
            continue
        candidates.append(_markdown_candidate(root, path, _doc_type(relative, settings), settings))
    return candidates


def scan_source_artifacts(
    root: Path,
    settings: ScannerSettings,
    *,
    excluded_roots: Sequence[Path] = (),
) -> list[Artifact]:
    """Scan configured scripts and optional source Markdown artifacts."""

    root = root.expanduser().resolve()
    return _dedupe_candidates(
        [
            *_script_candidates(root, settings),
            *_source_markdown_candidates(root, settings, excluded_roots),
        ]
    )


def _load_json(path: Path) -> Any:
    text = _safe_read_text(path, max_chars=2_000_000)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []


def _cron_candidates(root: Path, hermes_home: Path, settings: ScannerSettings) -> list[_Candidate]:
    jobs_path = hermes_home / "cron" / "jobs.json"
    payload = _load_json(jobs_path)
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        return []
    if not isinstance(jobs, list):
        return []

    candidates: list[_Candidate] = []
    registry_identity = _source_identity(jobs_path)
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "")
        name = str(job.get("name") or job_id or "unnamed-cron")
        prompt = str(job.get("prompt") or "")
        schedule = str(job.get("schedule_display") or job.get("schedule") or "")
        script = str(job.get("script") or "")
        skills = _string_list(job.get("skills"))
        enabled_toolsets = _string_list(job.get("enabled_toolsets"))
        summary = _first_sentence(prompt) or f"Cron job {name}"
        related = [*(f"skill:{_slugify(skill)}" for skill in skills)]
        if script:
            related.append(script)
        related.extend(_extract_paths(prompt))
        metadata_terms = _identifier_terms(
            name,
            schedule,
            script,
            " ".join(skills),
            " ".join(enabled_toolsets),
            prompt[:4_000],
            known_entities=settings.known_entities,
        )
        artifact = Artifact(
            id=f"cron:{_slugify(name or job_id)}",
            type="cron_job",
            title=name,
            path=f"{_display_path(jobs_path)}#{job_id or _slugify(name)}",
            summary=(
                f"{summary} Schedule: {schedule}. State: {job.get('state') or 'unknown'}. "
                f"Last status: {job.get('last_status') or 'unknown'}."
            )[:700],
            triggers=_significant_words(
                name,
                summary,
                schedule,
                script,
                " ".join(skills),
                " ".join(enabled_toolsets),
                prompt[:4_000],
                " ".join(metadata_terms),
            ),
            entities=_extract_entities(
                name,
                summary,
                prompt[:20_000],
                script,
                " ".join(metadata_terms),
                known_entities=settings.known_entities,
            ),
            related=_unique(related),
            updated_at=str(job.get("updated_at") or job.get("created_at") or "") or None,
            source="hermes_cron_registry",
            search_text="\n".join(
                [
                    prompt,
                    schedule,
                    script,
                    " ".join(skills),
                    " ".join(enabled_toolsets),
                    " ".join(metadata_terms),
                ]
            ),
        )
        identity = (*registry_identity, "cron", job_id or _slugify(name))
        candidates.append(_Candidate(artifact, identity))
    return candidates


def _mcp_secret_name(value: str) -> bool:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value.lstrip("-"))
    parts = re.findall(r"[a-z0-9]+", separated.lower())
    if parts and parts[-1] in {"url", "uri", "endpoint", "file", "path"}:
        return False
    part_set = set(parts)
    compact = "".join(parts)
    secret_parts = ("token", "secret", "password", "passwd", "authorization", "credential", "credentials")
    return (
        bool(set(secret_parts) & part_set)
        or {"api", "key"} <= part_set
        or compact.endswith((*secret_parts, "apikey"))
    )


def _sanitize_mcp_header(value: str) -> str:
    match = re.match(r"^(?P<name>[^:=\r\n]+):(.*)$", value, re.DOTALL)
    if not match or not _mcp_secret_name(match.group("name")):
        return value
    return f"{match.group('name')}: <redacted>"


def _sanitize_mcp_url_userinfo(value: str) -> str:
    def sanitize_authority(match: re.Match[str]) -> str:
        authority = match.group("authority")
        if "@" not in authority:
            return match.group(0)
        return f"{match.group('prefix')}{authority.rsplit('@', 1)[1]}"

    return _MCP_URI_AUTHORITY_RE.sub(sanitize_authority, value)


def _sanitize_mcp_arg_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return "<redacted>"
    if isinstance(value, dict):
        output: list[str] = []
        for raw_name, raw_value in value.items():
            name = str(raw_name)
            if isinstance(raw_value, (dict, list, tuple, set)) or _mcp_secret_name(name):
                rendered = "<redacted>"
            elif name in {"--header", "-H"}:
                rendered = _sanitize_mcp_header(_sanitize_mcp_url_userinfo(str(raw_value)))
            else:
                rendered = _sanitize_mcp_arg_value(raw_value)
            output.append(f"{name}: {rendered}")
        return " ".join(output)

    text = _sanitize_mcp_url_userinfo(str(value))
    if "=" not in text:
        return _sanitize_mcp_header(text)

    segments = text.split("=")
    prefix: list[str] = []
    for segment in segments[:-1]:
        header_name, separator, _ = segment.partition(":")
        if separator and _mcp_secret_name(header_name):
            return "=".join([*prefix, f"{header_name}: <redacted>"])
        prefix.append(segment)
        if _mcp_secret_name(segment):
            return f"{'='.join(prefix)}=<redacted>"
    return "=".join([*prefix, _sanitize_mcp_header(segments[-1])])


def _parse_inline_mcp_args(value: str) -> list[str] | None:
    text = value.strip()
    if not text.startswith("["):
        return None
    output: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 1
    closed = False
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if quote == "'" and index + 1 < len(text) and text[index + 1] == "'":
                    current.append("'")
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == '"' and index + 1 < len(text):
                current.append(text[index + 1])
                index += 2
                continue
            else:
                current.append(char)
        elif char in {'"', "'"}:
            quote = char
        elif char == ",":
            output.append("".join(current).strip())
            current = []
        elif char == "]":
            output.append("".join(current).strip())
            index += 1
            closed = True
            break
        else:
            current.append(char)
        index += 1
    trailing = text[index:].strip()
    if quote or not closed or (trailing and not trailing.startswith("#")):
        return ["<redacted>"]
    return [item for item in output if item]


def _sanitize_mcp_args(args: Any) -> str:
    inline_values = _parse_inline_mcp_args(args) if isinstance(args, str) else None
    values = inline_values if inline_values is not None else args if isinstance(args, list) else [args] if args else []
    output: list[str] = []
    redact_next = False
    for value in values:
        if redact_next:
            output.append("<redacted>")
            redact_next = False
            continue
        rendered = _sanitize_mcp_arg_value(value)
        output.append(rendered)
        if isinstance(value, str):
            redact_next = value.startswith("-") and "=" not in value and _mcp_secret_name(value)
    return " ".join(output)


def _load_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:  # type: ignore[attr-defined]
        return None


def _parse_mcp_servers_fallback(text: str) -> dict[str, tuple[dict[str, Any], str]]:
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


def _mcp_candidates(root: Path, hermes_home: Path, settings: ScannerSettings) -> list[_Candidate]:
    config_path = hermes_home / "config.yaml"
    text = _safe_read_text(config_path, max_chars=200_000)
    if not text:
        return []
    config = _load_yaml(text)
    servers: dict[str, tuple[Any, str]] = {}
    if isinstance(config, dict):
        mcp = config.get("mcp")
        if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
            servers.update((str(name), (data, "mcp.servers")) for name, data in mcp["servers"].items())
        native = config.get("mcp_servers")
        if isinstance(native, dict):
            servers.update((str(name), (data, "mcp_servers")) for name, data in native.items())
    if not servers:
        servers = _parse_mcp_servers_fallback(text)

    candidates: list[_Candidate] = []
    config_identity = _source_identity(config_path)
    for name, (raw_data, config_key) in sorted(servers.items()):
        data = raw_data if isinstance(raw_data, dict) else {}
        command = str(data.get("command") or "")
        url = _sanitize_mcp_url_userinfo(str(data.get("url") or data.get("base_url") or ""))
        args_text = _sanitize_mcp_args(data.get("args") or [])
        env = data.get("env") or {}
        env_text = " ".join(str(key) for key in env) if isinstance(env, dict) else ""
        summary_bits = [
            bit
            for bit in (
                f"command {command}" if command else "",
                f"url {url}" if url else "",
                args_text,
            )
            if bit
        ]
        summary = f"Hermes MCP server {name}: " + (
            "; ".join(summary_bits) if summary_bits else "configured in Hermes config"
        )
        metadata_terms = _identifier_terms(
            name,
            summary,
            command,
            url,
            args_text,
            env_text,
            known_entities=settings.known_entities,
        )
        artifact = Artifact(
            id=f"mcp:{_slugify(name)}",
            type="mcp_server",
            title=name,
            path=f"{_display_path(config_path)}#{config_key}.{name}",
            summary=summary[:700],
            triggers=_significant_words(
                name,
                summary,
                command,
                url,
                args_text,
                env_text,
                " ".join(metadata_terms),
            ),
            entities=_extract_entities(
                name,
                summary,
                command,
                url,
                args_text,
                env_text,
                " ".join(metadata_terms),
                known_entities=settings.known_entities,
            ),
            related=_extract_paths(" ".join([command, args_text, url])),
            source="hermes_config",
            search_text="\n".join([summary, command, url, args_text, env_text, " ".join(metadata_terms)]),
        )
        candidates.append(_Candidate(artifact, (*config_identity, "mcp", name)))
    return candidates


def scan_runtime_artifacts(
    root: Path,
    hermes_home: Path,
    settings: ScannerSettings,
) -> list[Artifact]:
    """Scan Hermes cron registry entries and MCP server configuration."""

    root = root.expanduser().resolve()
    hermes_home = hermes_home.expanduser().resolve()
    return _dedupe_candidates(
        [
            *_cron_candidates(root, hermes_home, settings),
            *_mcp_candidates(root, hermes_home, settings),
        ]
    )


def _tool_okf_candidates(okf_root: Path | None, root: Path, settings: ScannerSettings) -> list[_Candidate]:
    if okf_root is None or not okf_root.exists():
        return []
    candidates: list[_Candidate] = []
    for path in _iter_files(
        okf_root,
        suffixes={".md"},
        allowed_roots=(okf_root,),
        followlinks=False,
        excluded_dir_names=settings.exclude_dir_names,
    ):
        text = _safe_read_text(path)
        frontmatter = _parse_frontmatter(text)
        if str(frontmatter.get("artifact_type") or "tool_okf").strip() != "tool_okf":
            continue
        tool = str(frontmatter.get("tool") or "").strip()
        schema_hash = str(frontmatter.get("schema_hash") or "").strip()
        if not tool or not schema_hash:
            continue
        toolset = str(frontmatter.get("toolset") or "").strip()
        aliases = _frontmatter_list(frontmatter, "aliases")
        declared_triggers = _frontmatter_list(frontmatter, "triggers")
        related_tools = _frontmatter_list(frontmatter, "related_tools")
        title = str(frontmatter.get("title") or f"Tool OKF: {tool}").strip()
        metadata_terms = _identifier_terms(
            tool,
            toolset,
            title,
            " ".join(aliases),
            " ".join(declared_triggers),
            known_entities=settings.known_entities,
        )
        derived_triggers = _significant_words(
            tool,
            toolset,
            title,
            " ".join(aliases),
            " ".join(declared_triggers),
            " ".join(metadata_terms),
        )
        artifact = Artifact(
            id=f"tool_okf:{_slugify(tool)}",
            type="tool_okf",
            title=title,
            path=_display_path(path, root=root),
            summary=title,
            triggers=_unique([*aliases, *declared_triggers, *derived_triggers]),
            entities=_extract_entities(
                tool,
                toolset,
                title,
                " ".join(aliases),
                " ".join(declared_triggers),
                known_entities=settings.known_entities,
            ),
            related=_unique(f"tool_okf:{_slugify(item)}" for item in related_tools),
            updated_at=str(frontmatter.get("generated_at") or "") or None,
            source="generated_tool_okf",
            search_text="\n".join(
                [
                    tool,
                    toolset,
                    title,
                    title,
                    " ".join(aliases),
                    " ".join(declared_triggers),
                    " ".join(metadata_terms),
                ]
            ),
        )
        candidates.append(_candidate(artifact, path))
    return candidates


def scan_tool_okfs(
    okf_root: Path | None,
    root: Path,
    settings: ScannerSettings,
) -> list[Artifact]:
    """Scan validated generated tool-routing Markdown artifacts."""

    root = root.expanduser().resolve()
    return _dedupe_candidates(_tool_okf_candidates(okf_root, root, settings))


def collect_artifacts(
    root: Path,
    hermes_home: Path,
    settings: ScannerSettings,
    *,
    okf_root: Path | None = None,
) -> list[Artifact]:
    """Collect all four scanner families and de-duplicate IDs/source identities."""

    root = root.expanduser().resolve()
    hermes_home = hermes_home.expanduser().resolve()
    excluded_roots: tuple[Path, ...] = (okf_root,) if okf_root is not None else ()
    candidates = [
        *_skill_candidates(root, hermes_home, settings),
        *_custom_support_candidates(root, settings),
        *_runtime_support_candidates(root, hermes_home, settings),
        *_script_candidates(root, settings),
        *_source_markdown_candidates(root, settings, excluded_roots),
        *_cron_candidates(root, hermes_home, settings),
        *_mcp_candidates(root, hermes_home, settings),
        *_tool_okf_candidates(okf_root, root, settings),
    ]
    return _dedupe_candidates(candidates)


def _resolve_related(
    related: str,
    by_id: dict[str, Artifact],
    by_basename: dict[str, list[str]],
    by_display_path: dict[str, list[str]],
) -> str | None:
    clean = related.strip()
    if clean in by_id:
        return clean
    if ":" not in clean:
        skill_id = f"skill:{_slugify(clean)}"
        if skill_id in by_id:
            return skill_id
    normalized = clean.replace(str(Path.home()), "~")
    path_matches = by_display_path.get(normalized, [])
    if len(path_matches) == 1:
        return path_matches[0]
    basename_matches = by_basename.get(_portable_basename(clean), [])
    if len(basename_matches) == 1:
        return basename_matches[0]
    return None


def _dedupe_edges(edges: Iterable[Edge]) -> list[Edge]:
    by_key: dict[tuple[str, str, str], Edge] = {}
    for edge in edges:
        by_key.setdefault((edge.source, edge.target, edge.kind), edge)
    return sorted(by_key.values(), key=lambda item: (item.source, item.kind, item.target, item.evidence))


def build_edges(artifacts: Sequence[Artifact]) -> list[Edge]:
    """Build explicit relations and deterministic skill keyword-overlap edges.

    Keyword candidates are generated from an inverted term map.  This avoids a
    full skills-by-artifacts nested scan while preserving the existing rule:
    skills relate to scripts, runbooks, and memory documents sharing two terms.
    """

    ordered = sorted(artifacts, key=lambda artifact: artifact.id)
    by_id = {artifact.id: artifact for artifact in ordered}
    by_basename: dict[str, list[str]] = {}
    by_display_path: dict[str, list[str]] = {}
    for artifact in ordered:
        path = artifact.path.split("#", 1)[0]
        by_display_path.setdefault(path, []).append(artifact.id)
        by_basename.setdefault(_portable_basename(path), []).append(artifact.id)

    edges: list[Edge] = []
    for artifact in ordered:
        for related in artifact.related:
            target = _resolve_related(related, by_id, by_basename, by_display_path)
            if target and target != artifact.id:
                edges.append(Edge(artifact.id, target, "related_to", related))

    skill_postings: dict[str, list[str]] = {}
    target_postings: dict[str, list[str]] = {}
    for artifact in ordered:
        terms = set(_significant_words(artifact.title, artifact.summary, " ".join(artifact.triggers), limit=20))
        terms.difference_update(_STOPWORDS)
        if artifact.type == "skill":
            for term in terms:
                skill_postings.setdefault(term, []).append(artifact.id)
        elif artifact.type in {"script", "runbook", "memory_doc"}:
            for term in terms:
                target_postings.setdefault(term, []).append(artifact.id)

    overlap_by_pair: dict[tuple[str, str], set[str]] = {}
    for term in sorted(skill_postings.keys() & target_postings.keys()):
        for skill_id in sorted(skill_postings[term]):
            for target_id in sorted(target_postings[term]):
                if skill_id != target_id:
                    overlap_by_pair.setdefault((skill_id, target_id), set()).add(term)
    for (skill_id, target_id), overlap in sorted(overlap_by_pair.items()):
        if len(overlap) >= 2:
            edges.append(Edge(skill_id, target_id, "keyword_overlap", ",".join(sorted(overlap)[:5])))

    return _dedupe_edges(edges)
