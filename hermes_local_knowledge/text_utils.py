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
        full_title_match,
        -specific_title_hits,
        -specific_path_hits,
        -specific_trigger_hits,
        -specific_summary_hits,
        -title_hits,
        -path_hits,
        -trigger_hits,
        -summary_hits,
        -entity_hits,
        type_priority(str(row.get("type") or "")),
        float(row.get("rank") or 0.0),
        str(row.get("title") or ""),
    )
