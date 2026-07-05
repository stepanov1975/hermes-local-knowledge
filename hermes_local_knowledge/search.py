"""Search helpers for the SQLite-backed local knowledge index."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .storage import connect_readonly, decode_artifact_row
from .text_utils import fts_query, query_terms, search_sort_key


def _query_rows(conn: Any, match: str, candidate_limit: int) -> list[Any]:
    return conn.execute(
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


def _support_doc_parent(row: dict[str, Any]) -> str | None:
    if row.get("type") != "skill_support_doc":
        return None
    for related in row.get("related") or []:
        if str(related).startswith("skill:"):
            return str(related)
    return None


def _diversify_support_docs(rows: list[dict[str, Any]], *, per_parent_limit: int = 3) -> list[dict[str, Any]]:
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


def search_index(db_path: Path, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    terms = query_terms(query)
    match = fts_query(query)
    if not match:
        return []
    conn = connect_readonly(db_path)
    try:
        candidate_limit = max(int(limit) * 10, 50)
        rows = _query_rows(conn, match, candidate_limit)
        if len(terms) > 1 and len(rows) < int(limit):
            relaxed_rows = _query_rows(conn, fts_query(query, operator="OR"), candidate_limit)
            seen_ids = {str(row["id"]) for row in rows}
            rows = [*rows, *(row for row in relaxed_rows if str(row["id"]) not in seen_ids)]
        decoded = [decode_artifact_row(row) for row in rows]
        decoded.sort(key=lambda row: search_sort_key(row, terms))
        decoded = _diversify_support_docs(decoded)
        return decoded[: int(limit)]
    finally:
        conn.close()
