"""Search helpers for the SQLite-backed local knowledge index."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .storage import connect_readonly, decode_artifact_row
from .text_utils import fts_query, query_terms, search_sort_key


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
