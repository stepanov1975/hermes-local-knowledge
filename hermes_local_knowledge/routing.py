"""Deterministic routing hints learned from explicit local feedback."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .index import _query_terms, sqlite_readonly_uri
from .telemetry import EXPLICIT_FEEDBACK_ORIGIN, IMPLICIT_FEEDBACK_ORIGIN

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
MIN_IMPLICIT_CONFIRMATIONS = 2
MAX_GENERIC_ARTIFACT_QUERIES = 5
SearchIndexFn = Callable[..., list[dict[str, Any]]]
ARTIFACT_TYPE_BY_ID_PREFIX = {
    "cron": "cron_job",
    "mcp": "mcp_server",
}
QUERY_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{2,}")
ROUTING_TRACE_METADATA_KEY = "_routing_trace"


@dataclass(frozen=True, slots=True)
class FeedbackRoute:
    """One current, explicitly accepted query-to-artifact route."""

    query: str
    artifact_id: str
    artifact_type: str
    terms: frozenset[str]
    feedback_id: int


class RouteOutcome(str, Enum):
    """Persisted outcome of applying one feedback route decision."""

    NONE = "none"
    ALREADY_FIRST = "already_first"
    PROMOTED_EXISTING = "promoted_existing"
    PROMOTED_RETRY = "promoted_retry"
    VERIFICATION_FAILED = "verification_failed"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Typed final routing result and replay provenance for one search."""

    rows: list[dict[str, Any]]
    outcome: RouteOutcome
    feedback_id: int | None
    artifact_id: str | None
    feedback_max_id: int | None


@dataclass(frozen=True, slots=True)
class SearchRoutingTrace:
    """Internal service-to-adapter trace removed before user-visible output."""

    baseline_ids: tuple[str, ...]
    decision: RouteDecision


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


def _query_token_count(query: str) -> int:
    return len(QUERY_TOKEN_PATTERN.findall(query))


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
    if _query_token_count(route.query) > _query_token_count(query):
        return None
    overlap = len(route.terms & query_terms)
    if overlap < MIN_OVERLAP_TERMS:
        return None
    coverage = overlap / len(route.terms)
    if coverage < MIN_ROUTE_COVERAGE:
        return None
    return (1, int(coverage * 1000), overlap, route.feedback_id)


def _feedback_route_snapshot(
    usage_db_path: Path,
    *,
    root: Path,
    query: str,
    artifact_type: str | None,
    min_confirmations: int = MIN_IMPLICIT_CONFIRMATIONS,
    max_generic_queries: int = MAX_GENERIC_ARTIFACT_QUERIES,
) -> tuple[FeedbackRoute | None, int | None]:
    """Read one root-scoped feedback snapshot and select its strongest route.

    Only the latest significant feedback for each normalized query/artifact pair
    is authoritative. Neutral ``other`` feedback is ignored. Results are scoped
    to the configured source root so test/probe telemetry cannot train live
    routing. The high-water includes every feedback row for this root, even rows
    irrelevant to today's matcher.

    Implicit rows (``origin='implicit'``) are considered only as a fallback
    channel below every explicit route and only after two gates: a
    query/artifact pair needs at least ``min_confirmations`` implicit
    confirmations, and an artifact confirmed for more than
    ``max_generic_queries`` distinct queries is treated as generic (frequency
    without specificity carries no routing signal).
    """

    current_terms = frozenset(_query_terms(query))
    if not usage_db_path.is_file():
        return None, None

    try:
        connection = _connect_readonly(usage_db_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            high_water_row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM feedback WHERE root = ?",
                (str(root),),
            ).fetchone()
            feedback_max_id = int(high_water_row[0])
            rows = connection.execute(
                """
                SELECT f.id,
                       f.rating,
                       COALESCE(NULLIF(TRIM(f.query), ''), e.query) AS effective_query,
                       f.artifact_id,
                       f.origin
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
            ).fetchall() if current_terms else []
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None, None

    latest: dict[tuple[str, str], FeedbackRoute | NegativeFeedback] = {}
    latest_implicit: dict[tuple[str, str], FeedbackRoute] = {}
    implicit_counts: dict[tuple[str, str], int] = {}
    implicit_artifact_queries: dict[str, set[str]] = {}
    implicit_candidates: list[FeedbackRoute] = []
    for row in rows:
        accepted_query = str(row["effective_query"] or "").strip()
        accepted_terms = frozenset(_query_terms(accepted_query))
        artifact_id = str(row["artifact_id"] or "").strip()
        query_key = _feedback_query_key(accepted_query, accepted_terms)
        if not accepted_terms:
            continue
        origin = str(row["origin"] or EXPLICIT_FEEDBACK_ORIGIN)
        if origin == IMPLICIT_FEEDBACK_ORIGIN:
            if str(row["rating"]) != "useful":
                continue
            key = (query_key, artifact_id)
            implicit_counts[key] = implicit_counts.get(key, 0) + 1
            implicit_artifact_queries.setdefault(artifact_id, set()).add(query_key)
            prefix, separator, _name = artifact_id.partition(":")
            if not separator or not prefix:
                continue
            implicit_candidates.append(
                FeedbackRoute(
                    query=accepted_query,
                    artifact_id=artifact_id,
                    artifact_type=ARTIFACT_TYPE_BY_ID_PREFIX.get(prefix, prefix),
                    terms=accepted_terms,
                    feedback_id=int(row["id"]),
                )
            )
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
        prefix, separator, _name = artifact_id.partition(":")
        if not separator or not prefix:
            continue
        latest[key] = FeedbackRoute(
            query=accepted_query,
            artifact_id=artifact_id,
            artifact_type=ARTIFACT_TYPE_BY_ID_PREFIX.get(prefix, prefix),
            terms=accepted_terms,
            feedback_id=feedback_id,
        )

    for candidate in implicit_candidates:
        key = (candidate.query, candidate.artifact_id)
        query_key = _feedback_query_key(candidate.query, candidate.terms)
        counted_key = (query_key, candidate.artifact_id)
        if implicit_counts.get(counted_key, 0) < min_confirmations:
            continue
        if (
            len(implicit_artifact_queries.get(candidate.artifact_id, set()))
            > max_generic_queries
        ):
            continue
        if key in latest_implicit:
            continue
        latest_implicit[key] = candidate

    current_query_key = _feedback_query_key(query, current_terms)
    negative_feedback = [
        decision
        for decision in latest.values()
        if isinstance(decision, NegativeFeedback) and decision.query_key == current_query_key
    ]

    def _vetoed(route: FeedbackRoute) -> bool:
        return any(
            rejection.feedback_id > route.feedback_id
            and (not rejection.artifact_id or rejection.artifact_id == route.artifact_id)
            for rejection in negative_feedback
        )

    scored_explicit: list[tuple[tuple[int, int, int, int], FeedbackRoute]] = []
    for route in latest.values():
        if not isinstance(route, FeedbackRoute):
            continue
        if artifact_type is not None and route.artifact_type != artifact_type:
            continue
        if _vetoed(route):
            continue
        score = _match_score(route, query, current_terms)
        if score is not None:
            scored_explicit.append((score, route))
    if scored_explicit:
        scored_explicit.sort(key=lambda item: item[0], reverse=True)
        return scored_explicit[0][1], feedback_max_id

    scored_implicit: list[tuple[tuple[int, int, int, int], FeedbackRoute]] = []
    for route in latest_implicit.values():
        if artifact_type is not None and route.artifact_type != artifact_type:
            continue
        if _vetoed(route):
            continue
        score = _match_score(route, query, current_terms)
        if score is not None:
            # Implicit rows rank below every explicit route: exact 2→1, part 1→0.
            scored_implicit.append(
                ((score[0] - 1, score[1], score[2], score[3]), route)
            )
    if scored_implicit:
        scored_implicit.sort(key=lambda item: item[0], reverse=True)
        return scored_implicit[0][1], feedback_max_id
    return None, feedback_max_id


def best_feedback_route(
    usage_db_path: Path,
    *,
    root: Path,
    query: str,
    artifact_type: str | None,
    min_confirmations: int = MIN_IMPLICIT_CONFIRMATIONS,
    max_generic_queries: int = MAX_GENERIC_ARTIFACT_QUERIES,
) -> FeedbackRoute | None:
    """Return the strongest live route matching ``query``, or fail open."""

    route, _feedback_max_id = _feedback_route_snapshot(
        usage_db_path,
        root=root,
        query=query,
        artifact_type=artifact_type,
        min_confirmations=min_confirmations,
        max_generic_queries=max_generic_queries,
    )
    return route


def apply_feedback_route(
    rows: list[dict[str, Any]],
    *,
    route: FeedbackRoute | None,
    feedback_max_id: int | None,
    db_path: Path,
    limit: int,
    search_index_fn: SearchIndexFn,
) -> RouteDecision:
    """Promote one verified route and return complete typed provenance."""

    if route is None:
        return RouteDecision(
            rows=rows,
            outcome=RouteOutcome.NONE,
            feedback_id=None,
            artifact_id=None,
            feedback_max_id=feedback_max_id,
        )

    for position, row in enumerate(rows):
        if str(row.get("id") or "") != route.artifact_id:
            continue
        if position == 0:
            return RouteDecision(
                rows=rows,
                outcome=RouteOutcome.ALREADY_FIRST,
                feedback_id=route.feedback_id,
                artifact_id=route.artifact_id,
                feedback_max_id=feedback_max_id,
            )
        return RouteDecision(
            rows=[row, *rows[:position], *rows[position + 1 :]][:limit],
            outcome=RouteOutcome.PROMOTED_EXISTING,
            feedback_id=route.feedback_id,
            artifact_id=route.artifact_id,
            feedback_max_id=feedback_max_id,
        )

    try:
        retry_rows = search_index_fn(
            db_path,
            route.query,
            limit=max(limit, RETRY_LIMIT),
            artifact_type=route.artifact_type,
        )
    except Exception:
        return RouteDecision(
            rows=rows,
            outcome=RouteOutcome.VERIFICATION_FAILED,
            feedback_id=route.feedback_id,
            artifact_id=route.artifact_id,
            feedback_max_id=feedback_max_id,
        )
    verified = next(
        (
            row
            for row in retry_rows
            if str(row.get("id") or "") == route.artifact_id
        ),
        None,
    )
    if verified is None:
        return RouteDecision(
            rows=rows,
            outcome=RouteOutcome.VERIFICATION_FAILED,
            feedback_id=route.feedback_id,
            artifact_id=route.artifact_id,
            feedback_max_id=feedback_max_id,
        )
    return RouteDecision(
        rows=[
            verified,
            *(row for row in rows if str(row.get("id") or "") != route.artifact_id),
        ][:limit],
        outcome=RouteOutcome.PROMOTED_RETRY,
        feedback_id=route.feedback_id,
        artifact_id=route.artifact_id,
        feedback_max_id=feedback_max_id,
    )


def decide_feedback_route(
    rows: list[dict[str, Any]],
    *,
    usage_db_path: Path,
    root: Path,
    query: str,
    artifact_type: str | None,
    db_path: Path,
    limit: int,
    search_index_fn: SearchIndexFn,
    min_confirmations: int = MIN_IMPLICIT_CONFIRMATIONS,
    max_generic_queries: int = MAX_GENERIC_ARTIFACT_QUERIES,
) -> RouteDecision:
    """Read and apply one route while retaining the route snapshot high-water."""

    route, feedback_max_id = _feedback_route_snapshot(
        usage_db_path,
        root=root,
        query=query,
        artifact_type=artifact_type,
        min_confirmations=min_confirmations,
        max_generic_queries=max_generic_queries,
    )
    return apply_feedback_route(
        rows,
        route=route,
        feedback_max_id=feedback_max_id,
        db_path=db_path,
        limit=limit,
        search_index_fn=search_index_fn,
    )
