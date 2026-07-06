"""Text parsing, normalization, and ranking helpers."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .constants import DEFAULT_KNOWN_ENTITIES, QUERY_STOPWORDS, ROUTING_HINT_TERMS, STOPWORDS


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

def relative_config_parts(value: str) -> tuple[str, ...]:
    """Return normalized relative path parts from a scanner config entry."""
    return tuple(part for part in Path(str(value)).parts if part not in ("", "."))

def relpath_matches_config_dir(rel: Path, configured_dirs: Sequence[str]) -> bool:
    """True when a relative path is inside one configured source directory."""
    rel_parts = rel.parts
    for configured in configured_dirs:
        parts = relative_config_parts(configured)
        if parts and rel_parts[: len(parts)] == parts:
            return True
    return False

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


def _split_identifier(value: str) -> list[str]:
    pieces: list[str] = []
    for segment in re.split(r"[^A-Za-z0-9]+", value):
        if not segment:
            continue
        spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", segment)
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", spaced)
        pieces.extend(part.lower() for part in spaced.split() if part)
    return pieces


def _known_entity_aliases(known_entities: Sequence[str] | None) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for entity in known_entities or ():
        words = _split_identifier(entity)
        if not words:
            continue
        expansions = unique_preserve_order([*words, "".join(words), entity.lower()])
        keys: set[str] = set()
        compact = "".join(words)
        if compact:
            keys.add(compact)
        if len(words) == 1:
            keys.add(words[0])
        if len(words) > 1:
            acronym = "".join(word[0] for word in words if word)
            if len(acronym) >= 2:
                keys.add(acronym)
        keys.add(entity.lower().replace(" ", ""))
        for key in keys:
            aliases.setdefault(key, [])
            aliases[key].extend(expansions)
    return {key: unique_preserve_order(values) for key, values in aliases.items()}


def identifier_terms(*parts: str, known_entities: Sequence[str] | None = None, limit: int = 80) -> list[str]:
    """Expand path/code identifiers into searchable words without LLMs.

    This bridges text-poor operational artifacts whose only useful clues are
    names like ``ha_mcp`` or ``HOMEASSISTANT_URL``. Known multi-word entities
    also provide deterministic acronym/compact aliases: if ``Home Assistant``
    is configured as a known entity, ``ha`` and ``homeassistant`` expand to
    ``home assistant homeassistant``.
    """

    aliases = _known_entity_aliases(known_entities)
    terms: list[str] = []
    for part in parts:
        for raw in re.findall(r"[A-Za-z][A-Za-z0-9_./:+-]*", part):
            for token in _split_identifier(raw):
                if not token:
                    continue
                terms.append(token)
                terms.extend(aliases.get(token, []))
    return unique_preserve_order(term for term in terms if term and term not in STOPWORDS)[:limit]


def extract_env_names(text: str, *, limit: int = 80) -> list[str]:
    """Extract environment/config variable names without capturing values."""

    names: list[str] = []
    env_name = r"[A-Z_][A-Z0-9_]{2,}"
    string_name = r"[A-Za-z_][A-Za-z0-9_]*"
    patterns = [
        rf"(?:^|[\s;&])(?:export\s+)?({env_name})\s*=",
        rf"\$\{{?({env_name})",
        rf"os\.environ\s*\[\s*[\"']({string_name})[\"']\s*\]",
        rf"os\.(?:environ(?:\.get)?|getenv)\s*\(\s*[\"']({string_name})[\"']",
        rf"process\.env\.({string_name})",
        rf"process\.env\s*\[\s*[\"']({string_name})[\"']\s*\]",
    ]
    for pattern in patterns:
        names.extend(match.group(1) for match in re.finditer(pattern, text, re.MULTILINE))
    return unique_preserve_order(names)[:limit]


def extract_code_identifiers(text: str, *, limit: int = 80) -> list[str]:
    """Extract safe code symbols for routing without indexing literal values."""

    names: list[str] = []
    patterns = [
        r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*import\s+([A-Za-z_][A-Za-z0-9_.]*)",
        r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+([A-Za-z_][A-Za-z0-9_.*]*)",
        r"--([A-Za-z][A-Za-z0-9_-]{2,})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.MULTILINE):
            names.extend(group for group in match.groups() if group)
    return unique_preserve_order(names)[:limit]


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

def high_signal_terms(terms: Sequence[str]) -> list[str]:
    """Return query terms that are likely domain intent rather than routing hints."""

    specific = [term for term in terms if term not in ROUTING_HINT_TERMS]
    return specific or list(terms)

def identity_match_tier(row: dict[str, Any], terms: Sequence[str]) -> int:
    """Return a bounded priority tier for artifact identity matches.

    Local knowledge routes to whole artifacts. When a query clearly names an
    artifact, matches in identifiers, titles, and filenames should beat prose
    mentions. Keep the boost narrow: single-token or generic path overlap is not
    enough, otherwise broad queries such as ``backup strategy`` would be hijacked
    by a file merely named ``backup.sh``.
    """

    if not terms:
        return 3

    artifact_id = str(row.get("id") or "")
    title = str(row.get("title") or "")
    path = str(row.get("path") or "")
    path_name = Path(path).name
    path_stem = Path(path).stem
    query_compact = "".join(terms)

    if len(terms) >= 2 and query_compact:
        for value in (artifact_id, title, path_name, path_stem):
            if "".join(query_terms(value, drop_stopwords=False)) == query_compact:
                return 0

    identity_terms = [term for term in terms if term not in ROUTING_HINT_TERMS]
    if len(identity_terms) < 2:
        return 3

    title_tokens = set(query_terms(" ".join([artifact_id, title]), drop_stopwords=False))
    basename_tokens = set(query_terms(" ".join([path_name, path_stem]), drop_stopwords=False))
    path_tokens = set(query_terms(path, drop_stopwords=False))
    if token_hits(title_tokens, identity_terms) == len(identity_terms):
        return 1
    if token_hits(basename_tokens, identity_terms) == len(identity_terms):
        return 1
    if token_hits(path_tokens, identity_terms) == len(identity_terms):
        return 2
    return 3

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
    specific_terms = high_signal_terms(terms)
    identity_tier = identity_match_tier(row, terms)
    title_source = " ".join([str(row.get("id") or ""), str(row.get("title") or "")])
    title_tokens = set(query_terms(title_source, drop_stopwords=False))
    path_tokens = set(query_terms(str(row.get("path") or ""), drop_stopwords=False))
    trigger_source = " ".join(row.get("triggers") or [])
    trigger_tokens = set(query_terms(trigger_source, drop_stopwords=False))
    summary_tokens = set(query_terms(str(row.get("summary") or ""), drop_stopwords=False))
    entity_tokens = set(query_terms(" ".join(row.get("entities") or []), drop_stopwords=False))
    specific_title_hits = token_hits(title_tokens, specific_terms)
    specific_path_hits = token_hits(path_tokens, specific_terms)
    specific_trigger_hits = token_hits(trigger_tokens, specific_terms)
    specific_summary_hits = token_hits(summary_tokens, specific_terms)
    title_hits = token_hits(title_tokens, terms)
    path_hits = token_hits(path_tokens, terms)
    trigger_hits = token_hits(trigger_tokens, terms)
    summary_hits = token_hits(summary_tokens, terms)
    entity_hits = token_hits(entity_tokens, terms)
    full_title_match = 0 if terms and title_hits == len(terms) else 1
    return (
        identity_tier,
        full_title_match,
        -specific_title_hits,
        -specific_trigger_hits,
        -specific_summary_hits,
        -specific_path_hits,
        -title_hits,
        -trigger_hits,
        -summary_hits,
        -path_hits,
        -entity_hits,
        type_priority(str(row.get("type") or "")),
        float(row.get("rank") or 0.0),
        str(row.get("title") or ""),
    )
