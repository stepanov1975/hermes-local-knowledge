"""Search helpers for the SQLite-backed local knowledge index."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .constants import ROUTING_HINT_TERMS
from .storage import connect_readonly, decode_artifact_row
from .text_utils import fts_query, high_signal_terms, query_terms, search_sort_key, token_hits


FTS_BM25_WEIGHTS = "0.0, 0.2, 6.0, 1.0, 3.0, 2.0, 5.0, 0.4"


def _query_rows(conn: Any, match: str, candidate_limit: int, artifact_type: str = "") -> list[Any]:
    where = "artifact_fts MATCH ?"
    params: list[Any] = [match]
    if artifact_type:
        where += " AND a.type = ?"
        params.append(artifact_type)
    params.append(candidate_limit)
    return conn.execute(
        f"""
        SELECT a.*, bm25(artifact_fts, {FTS_BM25_WEIGHTS}) AS rank,
               CASE a.type
                 WHEN 'skill' THEN 0
                 WHEN 'script' THEN 1
                 WHEN 'cron_job' THEN 2
                 WHEN 'mcp_server' THEN 3
                 WHEN 'memory_doc' THEN 4
                 WHEN 'runbook' THEN 5
                 WHEN 'tool_okf' THEN 6
                 ELSE 7
               END AS type_priority
        FROM artifact_fts
        JOIN artifacts a ON a.id = artifact_fts.id
        WHERE {where}
        ORDER BY rank, type_priority, a.title
        LIMIT ?
        """,
        params,
    ).fetchall()


def _type_priority_sql(alias: str = "a") -> str:
    return f"""
               CASE {alias}.type
                 WHEN 'skill' THEN 0
                 WHEN 'script' THEN 1
                 WHEN 'cron_job' THEN 2
                 WHEN 'mcp_server' THEN 3
                 WHEN 'memory_doc' THEN 4
                 WHEN 'runbook' THEN 5
                 WHEN 'tool_okf' THEN 6
                 ELSE 7
               END AS type_priority
    """


def _metadata_rows(conn: Any, terms: list[str], candidate_limit: int, artifact_type: str = "") -> list[Any]:
    """Return artifacts matching query terms in structured metadata fields.

    FTS is still the primary retrieval path. This secondary path catches rows
    whose strong routing evidence is in normalized artifact metadata and keeps
    SQL's pre-limit step from becoming the only ranking decision.
    """

    candidate_terms = [term for term in high_signal_terms(terms) if term][:8]
    if not candidate_terms:
        return []

    field_weights = (
        ("a.id", 5),
        ("a.title", 5),
        ("a.path", 4),
        ("a.triggers_json", 2),
        ("a.entities_json", 2),
        ("a.summary", 1),
    )
    fields = tuple(field for field, _weight in field_weights)
    term_clauses: list[str] = []
    params: list[str] = []
    score_parts: list[str] = []
    score_params: list[str] = []
    for term in candidate_terms:
        like = f"%{term.lower()}%"
        term_clauses.append("(" + " OR ".join(f"lower({field}) LIKE ?" for field in fields) + ")")
        params.extend([like] * len(fields))
        for field, weight in field_weights:
            score_parts.append(f"CASE WHEN lower({field}) LIKE ? THEN {weight} ELSE 0 END")
            score_params.append(like)

    where_sql = "(" + " OR ".join(term_clauses) + ")"
    if artifact_type:
        where_sql += " AND a.type = ?"
        params.append(artifact_type)

    rows = conn.execute(
        f"""
        SELECT a.*, 0.0 AS rank, {_type_priority_sql("a")},
               ({" + ".join(score_parts)}) AS metadata_score
        FROM artifacts a
        WHERE {where_sql}
        ORDER BY metadata_score DESC, type_priority, a.title
        LIMIT ?
        """,
        [*score_params, *params, candidate_limit],
    ).fetchall()
    return rows


def _identity_metadata_rows(conn: Any, terms: list[str], candidate_limit: int, artifact_type: str = "") -> list[Any]:
    """Return rows whose artifact identity matches all non-routing query terms."""

    identity_terms = [term for term in terms if term not in ROUTING_HINT_TERMS]
    if len(identity_terms) < 2:
        return []

    fields = ("a.id", "a.title", "a.path")
    term_clauses: list[str] = []
    params: list[str] = []
    score_parts: list[str] = []
    score_params: list[str] = []
    for term in identity_terms:
        like = f"%{term.lower()}%"
        term_clauses.append("(" + " OR ".join(f"lower({field}) LIKE ?" for field in fields) + ")")
        params.extend([like] * len(fields))
        for field, weight in (("a.id", 5), ("a.title", 5), ("a.path", 4)):
            score_parts.append(f"CASE WHEN lower({field}) LIKE ? THEN {weight} ELSE 0 END")
            score_params.append(like)

    where_sql = " AND ".join(term_clauses)
    if artifact_type:
        where_sql += " AND a.type = ?"
        params.append(artifact_type)

    return conn.execute(
        f"""
        SELECT a.*, 0.0 AS rank, {_type_priority_sql("a")},
               ({" + ".join(score_parts)}) AS metadata_score
        FROM artifacts a
        WHERE {where_sql}
        ORDER BY metadata_score DESC, type_priority, a.title
        LIMIT ?
        """,
        [*score_params, *params, candidate_limit],
    ).fetchall()


def _merge_candidate_rows(*row_groups: list[Any]) -> list[Any]:
    rows: list[Any] = []
    seen: set[str] = set()
    for group in row_groups:
        for row in group:
            artifact_id = str(row["id"])
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            rows.append(row)
    return rows


def _fetch_artifacts(conn: Any, artifact_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not artifact_ids:
        return {}
    placeholders = ", ".join("?" for _ in artifact_ids)
    rows = conn.execute(
        f"SELECT a.*, 0.0 AS rank FROM artifacts a WHERE a.id IN ({placeholders})",
        artifact_ids,
    ).fetchall()
    return {str(row["id"]): decode_artifact_row(row) for row in rows}


def _support_doc_parent(row: dict[str, Any]) -> str | None:
    if row.get("type") != "skill_support_doc":
        return None
    for related in row.get("related") or []:
        if str(related).startswith("skill:"):
            return str(related)
    return None


def _diversify_support_docs(rows: list[dict[str, Any]], *, per_parent_limit: int = 1) -> list[dict[str, Any]]:
    """Avoid flooding top results with many support docs from one parent skill.

    Support docs are excellent long-tail hits, but artifact routing is less useful
    when a generic class-level query returns only references from one skill and
    hides the parent skill/script/runbook that the agent should inspect first.
    """

    selected: list[dict[str, Any]] = []
    support_counts: dict[str, int] = {}
    for row in rows:
        parent = _support_doc_parent(row)
        if parent is None:
            selected.append(row)
            continue
        count = support_counts.get(parent, 0)
        if count >= per_parent_limit:
            continue
        support_counts[parent] = count + 1
        selected.append(row)
    return selected


def _lift_support_doc_parents(conn: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Place owning skills next to matching support docs.

    A support doc hit is strong evidence for its parent skill. For routing, the
    parent is usually the artifact to load first, while the support doc remains
    adjacent as the specific evidence trail.
    """

    existing = {str(row.get("id")): row for row in rows}
    missing_parent_ids = []
    seen_missing: set[str] = set()
    for row in rows:
        parent = _support_doc_parent(row)
        if parent and parent not in existing and parent not in seen_missing:
            missing_parent_ids.append(parent)
            seen_missing.add(parent)
    parents = _fetch_artifacts(conn, missing_parent_ids)

    output: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for row in rows:
        parent = _support_doc_parent(row)
        if parent and parent not in emitted:
            parent_row = existing.get(parent) or parents.get(parent)
            if parent_row is not None:
                output.append(parent_row)
                emitted.add(parent)
        artifact_id = str(row.get("id"))
        if artifact_id not in emitted:
            output.append(row)
            emitted.add(artifact_id)
    return output


def _requested_operational_types(terms: list[str]) -> set[str]:
    requested: set[str] = set()
    term_set = set(terms)
    if "script" in term_set:
        requested.add("script")
    if "cron" in term_set or "job" in term_set or "jobs" in term_set:
        requested.add("cron_job")
    if "mcp" in term_set or "wrapper" in term_set:
        requested.add("mcp_server")
        requested.add("script")
    return requested


OPERATIONAL_INTENT_TERMS = {"script", "cron", "job", "jobs", "mcp", "wrapper"}
PROSE_ARTIFACT_TYPES = {"doc", "runbook", "memory_doc"}
STRICT_REFERENCE_TYPES = {"skill", "skill_support_doc"}


def _operational_specific_terms(terms: list[str]) -> list[str]:
    """Return query terms that distinguish *which* operational artifact is wanted."""

    return [term for term in high_signal_terms(terms) if term not in OPERATIONAL_INTENT_TERMS]


def _specific_term_hit_count(row: dict[str, Any], specific_terms: list[str]) -> int:
    source = " ".join(
        [
            str(row.get("id") or ""),
            str(row.get("title") or ""),
            str(row.get("path") or ""),
            str(row.get("summary") or ""),
            " ".join(row.get("triggers") or []),
            " ".join(row.get("entities") or []),
        ]
    )
    tokens = set(query_terms(source, drop_stopwords=False))
    return token_hits(tokens, specific_terms)


def _row_matches_specific_terms(row: dict[str, Any], specific_terms: list[str]) -> bool:
    if not specific_terms:
        return True
    return _specific_term_hit_count(row, specific_terms) == len(specific_terms)


def _row_matches_any_specific_term(row: dict[str, Any], specific_terms: list[str]) -> bool:
    if not specific_terms:
        return True
    return _specific_term_hit_count(row, specific_terms) > 0


def _final_operational_sort_key(
    row: dict[str, Any],
    requested_types: set[str],
    strict_ids: set[str],
    specific_terms: list[str],
    original_position: int,
) -> tuple[int, int, int]:
    """Sort only the final mixed strict/fallback set for operational intent.

    Script-only queries keep strict skill/support-doc hits protected because many
    reusable skills are legitimately about helper scripts. Script rows still need
    at least one domain-specific term when such terms exist, so a generic script
    hit does not leapfrog stricter prose. Cron/MCP intent is more specific and
    should route to relevant operational artifacts first, while still keeping
    strict same-domain reference skills/docs above broad prose. In all cases,
    fallback skills/support docs do not leapfrog stricter prose rows merely
    because the query contains an operational word.
    """

    if not requested_types:
        return (0, 0, original_position)
    artifact_id = str(row.get("id") or "")
    artifact_type = str(row.get("type") or "")
    protect_strict_reference = requested_types == {"script"}
    if protect_strict_reference and artifact_id in strict_ids and artifact_type in STRICT_REFERENCE_TYPES:
        return (0, 0, original_position)
    if artifact_type in requested_types:
        if protect_strict_reference and _row_matches_any_specific_term(row, specific_terms):
            return (1, 0, original_position)
        if not protect_strict_reference and _row_matches_specific_terms(row, specific_terms):
            return (1, 0, original_position)
    if artifact_id in strict_ids and artifact_type in STRICT_REFERENCE_TYPES and _row_matches_specific_terms(
        row,
        specific_terms,
    ):
        return (2, 0, original_position)
    if artifact_type in PROSE_ARTIFACT_TYPES:
        return (3, 0, original_position)
    return (4, 0, original_position)


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        artifact_id = str(row.get("id"))
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        output.append(row)
    return output


def _rank_rows(
    conn: Any,
    rows: list[Any],
    terms: list[str],
    *,
    lift_parents: bool = True,
) -> list[dict[str, Any]]:
    decoded = [decode_artifact_row(row) for row in rows]
    decoded.sort(key=lambda row: search_sort_key(row, terms))
    lifted = _lift_support_doc_parents(conn, decoded) if lift_parents else decoded
    return _diversify_support_docs(_dedupe_rows(lifted))


def _finalize_results(
    rows: list[dict[str, Any]],
    output_limit: int,
    terms: list[str],
    *,
    requested_operational_types: set[str] | None = None,
    strict_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    selected = _diversify_support_docs(_dedupe_rows(rows))
    requested_types = requested_operational_types or set()
    strict_id_set = strict_ids or set()
    specific_terms = _operational_specific_terms(terms) if requested_types else []
    position_by_id = {str(row.get("id") or ""): index for index, row in enumerate(selected)}
    selected.sort(
        key=lambda row: _final_operational_sort_key(
            row,
            requested_types,
            strict_id_set,
            specific_terms,
            position_by_id.get(str(row.get("id") or ""), 0),
        )
    )
    return selected[:output_limit]


def _has_quoted_phrase(query: str) -> bool:
    """Return true only for balanced quoted phrases, not apostrophes."""

    return bool(re.search(r'(?<!\w)"[^"\n]+"(?!\w)|(?<!\w)\'[^\'\n]+\'(?!\w)', query))


def _is_quoted_only_query(query: str) -> bool:
    """Return true when the query contains quoted phrase(s) and no extra terms."""

    without_quoted = re.sub(r'(?<!\w)"[^"\n]+"(?!\w)|(?<!\w)\'[^\'\n]+\'(?!\w)', " ", query)
    return _has_quoted_phrase(query) and not query_terms(without_quoted)


def search_index(db_path: Path, query: str, *, limit: int = 10, artifact_type: str | None = None) -> list[dict[str, Any]]:
    terms = query_terms(query)
    match = fts_query(query)
    if not match:
        return []
    type_filter = str(artifact_type or "").strip()
    conn = connect_readonly(db_path)
    try:
        output_limit = int(limit)
        candidate_limit = max(output_limit * 20, 100)
        exact_query = _has_quoted_phrase(query)
        quoted_only_query = _is_quoted_only_query(query)
        lift_parents = not exact_query and not type_filter
        requested_operational_types = set() if exact_query else _requested_operational_types(terms)
        strict_rows = _query_rows(conn, match, candidate_limit, type_filter)
        strict = _rank_rows(
            conn,
            strict_rows,
            terms,
            lift_parents=lift_parents,
        )
        strict_ids = {str(row["id"]) for row in strict}
        if quoted_only_query:
            return _finalize_results(
                strict,
                output_limit,
                terms,
                requested_operational_types=requested_operational_types,
                strict_ids=strict_ids,
            )

        metadata_identity: list[dict[str, Any]] = []
        if not requested_operational_types:
            metadata_identity = _rank_rows(
                conn,
                [row for row in _identity_metadata_rows(conn, terms, candidate_limit, type_filter) if str(row["id"]) not in strict_ids],
                terms,
                lift_parents=lift_parents,
            )
        if len(strict) >= output_limit and not requested_operational_types and not metadata_identity:
            return _finalize_results(
                strict,
                output_limit,
                terms,
                requested_operational_types=requested_operational_types,
                strict_ids=strict_ids,
            )
        if len(strict) >= output_limit and not requested_operational_types and metadata_identity:
            return _finalize_results(
                [*metadata_identity, *strict],
                output_limit,
                terms,
                requested_operational_types=requested_operational_types,
                strict_ids=strict_ids,
            )

        metadata_identity_ids = {str(row["id"]) for row in metadata_identity}
        fallback_rows = _merge_candidate_rows(
            _query_rows(conn, fts_query(query, operator="OR"), candidate_limit, type_filter) if len(terms) > 1 else [],
            [] if metadata_identity else _metadata_rows(conn, terms, candidate_limit, type_filter),
        )
        fallback = _rank_rows(
            conn,
            [
                row
                for row in fallback_rows
                if str(row["id"]) not in strict_ids and str(row["id"]) not in metadata_identity_ids
            ],
            terms,
            lift_parents=lift_parents,
        )
        fallback = [
            row for row in fallback if str(row["id"]) not in strict_ids and str(row["id"]) not in metadata_identity_ids
        ]
        return _finalize_results(
            [*metadata_identity, *strict, *fallback] if metadata_identity else [*strict, *fallback],
            output_limit,
            terms,
            requested_operational_types=requested_operational_types,
            strict_ids=strict_ids,
        )
    finally:
        conn.close()
