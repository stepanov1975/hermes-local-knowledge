"""Deterministic routing hints learned from explicit local feedback."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .index import _query_terms, sqlite_readonly_uri

POSITIVE_FEEDBACK_RATINGS = frozenset({"great", "useful"})
NEGATIVE_FEEDBACK_RATINGS = frozenset(
    {"missing", "noisy", "not_useful", "stale", "wrong_artifact"}
)
SIGNIFICANT_FEEDBACK_RATINGS = POSITIVE_FEEDBACK_RATINGS | NEGATIVE_FEEDBACK_RATINGS
MIN_ROUTE_TERMS = 3
MIN_OVERLAP_TERMS = 3
MIN_ROUTE_COVERAGE = 0.75
FEEDBACK_SCAN_LIMIT = 1000
RETRY_LIMIT = 10
FEEDBACK_BUSY_TIMEOUT_SECONDS = 0.1
FEEDBACK_BUSY_TIMEOUT_MS = 100
SearchIndexFn = Callable[..., list[dict[str, Any]]]
ARTIFACT_TYPE_BY_ID_PREFIX = {
    "cron": "cron_job",
    "mcp": "mcp_server",
}


@dataclass(frozen=True, slots=True)
class FeedbackRoute:
    """One current, explicitly accepted query-to-artifact route."""

    query: str
    artifact_id: str
    artifact_type: str
    terms: frozenset[str]
    feedback_id: int


@dataclass(frozen=True, slots=True)
class NegativeFeedback:
    """One current explicit rejection used only to suppress an older route."""

    query_key: str
    artifact_id: str
    feedback_id: int


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        sqlite_readonly_uri(path),
        uri=True,
        timeout=FEEDBACK_BUSY_TIMEOUT_SECONDS,
    )
    connection.execute(f"PRAGMA busy_timeout={FEEDBACK_BUSY_TIMEOUT_MS}")
    return connection


def _normalized_query(query: str) -> str:
    return " ".join(query.casefold().split())


def _feedback_query_key(query: str, terms: frozenset[str]) -> str:
    if '"' in query:
        return f"quoted:{_normalized_query(query)}"
    return f"terms:{' '.join(sorted(terms))}"


def _match_score(
    route: FeedbackRoute,
    query: str,
    query_terms: frozenset[str],
) -> tuple[int, int, int, int] | None:
    if _normalized_query(route.query) == _normalized_query(query):
        return (2, len(route.terms), len(route.terms), route.feedback_id)
    if '"' in route.query or '"' in query:
        return None
    if len(route.terms) < MIN_ROUTE_TERMS:
        return None
    if len(route.terms) > len(query_terms):
        return None
    overlap = len(route.terms & query_terms)
    if overlap < MIN_OVERLAP_TERMS:
        return None
    coverage = overlap / len(route.terms)
    if coverage < MIN_ROUTE_COVERAGE:
        return None
    return (1, int(coverage * 1000), overlap, route.feedback_id)


def best_feedback_route(
    usage_db_path: Path,
    *,
    root: Path,
    query: str,
    artifact_type: str | None,
) -> FeedbackRoute | None:
    """Return the strongest live route matching ``query``, or fail open.

    Only the latest significant feedback for each normalized query/artifact pair
    is authoritative. Neutral ``other`` feedback is ignored. Results are scoped
    to the configured source root so test/probe telemetry cannot train live
    routing.
    """

    current_terms = frozenset(_query_terms(query))
    if not current_terms or not usage_db_path.is_file():
        return None

    try:
        connection = _connect_readonly(usage_db_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT f.id,
                       f.rating,
                       COALESCE(NULLIF(TRIM(f.query), ''), e.query) AS effective_query,
                       f.artifact_id
                FROM feedback AS f
                LEFT JOIN usage_events AS e ON e.id = f.event_id
                WHERE f.root = ?
                  AND f.rating IN (?, ?, ?, ?, ?, ?, ?)
                  AND COALESCE(NULLIF(TRIM(f.query), ''), e.query) IS NOT NULL
                ORDER BY f.id DESC
                LIMIT ?
                """,
                (
                    str(root),
                    *sorted(SIGNIFICANT_FEEDBACK_RATINGS),
                    FEEDBACK_SCAN_LIMIT,
                ),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None

    latest: dict[tuple[str, str], FeedbackRoute | NegativeFeedback] = {}
    for row in rows:
        accepted_query = str(row["effective_query"] or "").strip()
        accepted_terms = frozenset(_query_terms(accepted_query))
        artifact_id = str(row["artifact_id"] or "").strip()
        query_key = _feedback_query_key(accepted_query, accepted_terms)
        prefix, separator, _name = artifact_id.partition(":")
        if not accepted_terms:
            continue
        key = (query_key, artifact_id)
        if key in latest:
            continue
        feedback_id = int(row["id"])
        if str(row["rating"]) in NEGATIVE_FEEDBACK_RATINGS:
            latest[key] = NegativeFeedback(
                query_key=query_key,
                artifact_id=artifact_id,
                feedback_id=feedback_id,
            )
            continue
        if not separator or not prefix:
            continue
        latest[key] = FeedbackRoute(
            query=accepted_query,
            artifact_id=artifact_id,
            artifact_type=ARTIFACT_TYPE_BY_ID_PREFIX.get(prefix, prefix),
            terms=accepted_terms,
            feedback_id=feedback_id,
        )

    current_query_key = _feedback_query_key(query, current_terms)
    negative_feedback = [
        decision
        for decision in latest.values()
        if isinstance(decision, NegativeFeedback) and decision.query_key == current_query_key
    ]
    scored: list[tuple[tuple[int, int, int, int], FeedbackRoute]] = []
    for route in latest.values():
        if not isinstance(route, FeedbackRoute):
            continue
        if artifact_type is not None and route.artifact_type != artifact_type:
            continue
        if any(
            rejection.feedback_id > route.feedback_id
            and (not rejection.artifact_id or rejection.artifact_id == route.artifact_id)
            for rejection in negative_feedback
        ):
            continue
        score = _match_score(route, query, current_terms)
        if score is not None:
            scored.append((score, route))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def apply_feedback_route(
    rows: list[dict[str, Any]],
    *,
    route: FeedbackRoute,
    db_path: Path,
    limit: int,
    search_index_fn: SearchIndexFn,
) -> list[dict[str, Any]]:
    """Promote one verified route, retrying its accepted typed query if needed."""

    for position, row in enumerate(rows):
        if str(row.get("id") or "") != route.artifact_id:
            continue
        if position == 0:
            return rows
        return [row, *rows[:position], *rows[position + 1 :]][:limit]

    try:
        retry_rows = search_index_fn(
            db_path,
            route.query,
            limit=max(limit, RETRY_LIMIT),
            artifact_type=route.artifact_type,
        )
    except Exception:
        return rows
    verified = next(
        (
            row
            for row in retry_rows
            if str(row.get("id") or "") == route.artifact_id
        ),
        None,
    )
    if verified is None:
        return rows
    return [
        verified,
        *(row for row in rows if str(row.get("id") or "") != route.artifact_id),
    ][:limit]
