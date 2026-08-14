"""Deterministic routing hints learned from explicit and implicit local feedback."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
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
QUERY_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{2,}")
ROUTING_TRACE_METADATA_KEY = "_routing_trace"


@dataclass(frozen=True, slots=True)
class FeedbackRoute:
    """One current explicit or implicit query-to-artifact route."""

    query: str
    artifact_id: str
    artifact_type: str
    terms: frozenset[str]
    feedback_id: int | None


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
    implicit_feedback_max_id: int | None = None


@dataclass(frozen=True, slots=True)
class SearchRoutingTrace:
    """Internal service-to-adapter trace removed before user-visible output."""

    baseline_ids: tuple[str, ...]
    decision: RouteDecision


@dataclass(frozen=True, slots=True)
class NegativeFeedback:
    """One current explicit rejection used only to suppress an older route."""

    query_key: str
    query: str
    terms: frozenset[str]
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


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _persisted_id(value: Any, *, allow_zero: bool = False) -> int | None:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        return None
    return value


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
        return (2, len(route.terms), len(route.terms), route.feedback_id or 0)
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
    return (1, int(coverage * 1000), overlap, route.feedback_id or 0)


def _feedback_route_snapshot(
    usage_db_path: Path,
    *,
    root: Path,
    query: str,
    artifact_type: str | None,
    connection: sqlite3.Connection | None = None,
) -> tuple[FeedbackRoute | None, int | None, tuple[NegativeFeedback, ...]]:
    """Read one root-scoped feedback snapshot and select its strongest route.

    Only the latest significant feedback for each normalized query/artifact pair
    is authoritative. Neutral ``other`` feedback is ignored. Results are scoped
    to the configured source root so test/probe telemetry cannot train live
    routing. The high-water includes every feedback row for this root, even rows
    irrelevant to today's matcher.
    """

    current_terms = frozenset(_query_terms(query))
    if connection is None:
        if not usage_db_path.is_file():
            return None, None, ()
        try:
            with _connect_readonly(usage_db_path) as owned_connection:
                owned_connection.row_factory = sqlite3.Row
                owned_connection.execute("BEGIN")
                return _feedback_route_snapshot(
                    usage_db_path,
                    root=root,
                    query=query,
                    artifact_type=artifact_type,
                    connection=owned_connection,
                )
        except (OSError, sqlite3.Error):
            return None, None, ()

    high_water_row = connection.execute(
        "SELECT COALESCE(MAX(id), 0) FROM feedback WHERE root = ?",
        (str(root),),
    ).fetchone()
    feedback_max_id = _persisted_id(high_water_row[0], allow_zero=True)
    if feedback_max_id is None:
        return None, None, ()
    rows = (
        connection.execute(
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
        if current_terms
        else []
    )

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
        feedback_id = _persisted_id(row["id"])
        if feedback_id is None:
            continue
        if str(row["rating"]) in NEGATIVE_FEEDBACK_RATINGS:
            latest[key] = NegativeFeedback(
                query_key=query_key,
                query=accepted_query,
                terms=accepted_terms,
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
        if isinstance(decision, NegativeFeedback)
        and (
            decision.query_key == current_query_key
            or _match_score(
                FeedbackRoute(
                    query=decision.query,
                    artifact_id=decision.artifact_id,
                    artifact_type="",
                    terms=decision.terms,
                    feedback_id=decision.feedback_id,
                ),
                query,
                current_terms,
            )
            is not None
        )
    ]
    scored: list[tuple[tuple[int, int, int, int], FeedbackRoute]] = []
    for route in latest.values():
        if not isinstance(route, FeedbackRoute):
            continue
        if artifact_type is not None and route.artifact_type != artifact_type:
            continue
        if any(
            rejection.feedback_id > (route.feedback_id or 0)
            and (not rejection.artifact_id or rejection.artifact_id == route.artifact_id)
            for rejection in negative_feedback
        ):
            continue
        score = _match_score(route, query, current_terms)
        if score is not None:
            scored.append((score, route))
    if not scored:
        return None, feedback_max_id, tuple(negative_feedback)
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1], feedback_max_id, tuple(negative_feedback)


def _implicit_feedback_route_snapshot(
    usage_db_path: Path,
    *,
    root: Path,
    query: str,
    artifact_type: str | None,
    min_confirmations: int,
    max_generic_queries: int,
    connection: sqlite3.Connection | None = None,
) -> tuple[FeedbackRoute | None, int | None]:
    """Return one validated implicit route and its root-scoped snapshot high-water."""

    if connection is None:
        if not usage_db_path.is_file():
            return None, None
        try:
            with _connect_readonly(usage_db_path) as owned_connection:
                owned_connection.row_factory = sqlite3.Row
                owned_connection.execute("BEGIN")
                return _implicit_feedback_route_snapshot(
                    usage_db_path,
                    root=root,
                    query=query,
                    artifact_type=artifact_type,
                    min_confirmations=min_confirmations,
                    max_generic_queries=max_generic_queries,
                    connection=owned_connection,
                )
        except (OSError, sqlite3.Error):
            return None, None

    if not _has_table(connection, "implicit_feedback"):
        return None, None
    implicit_columns = _table_columns(connection, "implicit_feedback")
    if not {"id", "root"}.issubset(implicit_columns):
        return None, None
    high_water_row = connection.execute(
        "SELECT COALESCE(MAX(id), 0) FROM implicit_feedback WHERE root = ?",
        (str(root),),
    ).fetchone()
    implicit_feedback_max_id = _persisted_id(high_water_row[0], allow_zero=True)
    if implicit_feedback_max_id is None:
        return None, None
    if not {
        "search_event_id",
        "query",
        "artifact_id",
        "session_id",
        "task_id",
        "turn_id",
    }.issubset(implicit_columns) or not _has_table(connection, "usage_events"):
        return None, implicit_feedback_max_id
    usage_columns = _table_columns(connection, "usage_events")
    if not {
        "id",
        "tool",
        "success",
        "query",
        "baseline_top_ids_json",
        "top_ids_json",
        "session_id",
        "task_id",
        "turn_id",
        "root",
    }.issubset(usage_columns):
        return None, implicit_feedback_max_id
    rows = connection.execute(
        """
        SELECT i.id, i.query, i.artifact_id, i.search_event_id,
               i.session_id AS implicit_session_id,
               i.task_id AS implicit_task_id,
               i.turn_id AS implicit_turn_id,
               e.id AS event_id,
               e.tool AS event_tool, e.success AS event_success,
               e.query AS event_query,
               e.baseline_top_ids_json AS event_baseline_top_ids_json,
               e.top_ids_json AS event_top_ids_json,
               e.session_id AS event_session_id,
               e.task_id AS event_task_id,
               e.turn_id AS event_turn_id,
               e.root AS event_root
        FROM implicit_feedback i
        LEFT JOIN usage_events e ON e.id = i.search_event_id
        WHERE i.root = ?
        ORDER BY i.id DESC
        LIMIT ?
        """,
        (str(root), FEEDBACK_SCAN_LIMIT),
    ).fetchall()

    current_terms = frozenset(_query_terms(query))
    confirmations: dict[tuple[str, str], set[int]] = {}
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        try:
            baseline_ids = json.loads(str(row["event_baseline_top_ids_json"] or ""))
            final_ids = json.loads(str(row["event_top_ids_json"] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        route_query = str(row["query"] or "").strip()
        route_terms = frozenset(_query_terms(route_query))
        candidate_id = str(row["artifact_id"] or "").strip()
        implicit_session_id = str(row["implicit_session_id"] or "")
        implicit_task_id = str(row["implicit_task_id"] or "")
        implicit_turn_id = str(row["implicit_turn_id"] or "")
        if (
            not route_terms
            or not candidate_id
            or str(row["event_tool"] or "") != "knowledge_search"
            or type(row["event_success"]) is not int
            or row["event_success"] != 1
            or str(row["event_root"] or "") != str(root)
            or route_query != str(row["event_query"] or "")
            or not implicit_session_id
            or implicit_session_id != str(row["event_session_id"] or "")
            or not implicit_task_id
            or implicit_task_id != str(row["event_task_id"] or "")
            or not implicit_turn_id
            or implicit_turn_id != str(row["event_turn_id"] or "")
            or not isinstance(baseline_ids, list)
            or any(type(value) is not str or not value for value in baseline_ids)
            or candidate_id not in baseline_ids
            or not isinstance(final_ids, list)
            or any(type(value) is not str or not value for value in final_ids)
            or candidate_id not in final_ids
        ):
            continue
        implicit_feedback_id = _persisted_id(row["id"])
        search_event_id = _persisted_id(row["search_event_id"])
        event_id = _persisted_id(row["event_id"])
        if (
            implicit_feedback_id is None
            or search_event_id is None
            or event_id is None
            or search_event_id != event_id
        ):
            continue
        key = (_feedback_query_key(route_query, route_terms), candidate_id)
        confirmations.setdefault(key, set()).add(search_event_id)
        latest.setdefault(key, row)

    mature_keys = {
        key for key, search_events in confirmations.items() if len(search_events) >= min_confirmations
    }
    mature_queries_by_artifact: dict[str, int] = {}
    for _query_key, candidate_id in mature_keys:
        mature_queries_by_artifact[candidate_id] = mature_queries_by_artifact.get(candidate_id, 0) + 1

    candidates: list[tuple[tuple[int, int, int, int], FeedbackRoute]] = []
    for key in mature_keys:
        if mature_queries_by_artifact[key[1]] > max_generic_queries:
            continue
        row = latest[key]
        route_query = str(row["query"])
        candidate_id = str(row["artifact_id"])
        prefix, separator, _name = candidate_id.partition(":")
        if not separator or not prefix:
            continue
        inferred_type = ARTIFACT_TYPE_BY_ID_PREFIX.get(prefix, prefix)
        if artifact_type is not None and artifact_type != inferred_type:
            continue
        candidate_route = FeedbackRoute(
            query=route_query,
            artifact_id=candidate_id,
            artifact_type=inferred_type,
            terms=frozenset(_query_terms(route_query)),
            # Implicit IDs belong to another table and are not explicit
            # feedback provenance.
            feedback_id=None,
        )
        score = _match_score(candidate_route, query, current_terms)
        if score is not None:
            candidates.append((score, candidate_route))
    selected_route = (
        max(
            candidates,
            key=lambda item: (
                item[0],
                item[1].artifact_id,
                _normalized_query(item[1].query),
                item[1].query,
            ),
        )[1]
        if candidates
        else None
    )
    return selected_route, implicit_feedback_max_id


def _implicit_feedback_route(
    usage_db_path: Path,
    *,
    root: Path,
    query: str,
    artifact_type: str | None,
    min_confirmations: int,
    max_generic_queries: int,
) -> FeedbackRoute | None:
    """Return a route confirmed by distinct searches without generic overreach."""

    route, _implicit_feedback_max_id = _implicit_feedback_route_snapshot(
        usage_db_path,
        root=root,
        query=query,
        artifact_type=artifact_type,
        min_confirmations=min_confirmations,
        max_generic_queries=max_generic_queries,
    )
    return route


def best_feedback_route(
    usage_db_path: Path,
    *,
    root: Path,
    query: str,
    artifact_type: str | None,
) -> FeedbackRoute | None:
    """Return the strongest live route matching ``query``, or fail open."""

    route, _feedback_max_id, _negative_feedback = _feedback_route_snapshot(
        usage_db_path,
        root=root,
        query=query,
        artifact_type=artifact_type,
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
    implicit_feedback_max_id: int | None = None,
) -> RouteDecision:
    """Promote one verified route and return complete typed provenance."""

    if route is None:
        return RouteDecision(
            rows=rows,
            outcome=RouteOutcome.NONE,
            feedback_id=None,
            artifact_id=None,
            feedback_max_id=feedback_max_id,
            implicit_feedback_max_id=implicit_feedback_max_id,
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
                implicit_feedback_max_id=implicit_feedback_max_id,
            )
        return RouteDecision(
            rows=[row, *rows[:position], *rows[position + 1 :]][:limit],
            outcome=RouteOutcome.PROMOTED_EXISTING,
            feedback_id=route.feedback_id,
            artifact_id=route.artifact_id,
            feedback_max_id=feedback_max_id,
            implicit_feedback_max_id=implicit_feedback_max_id,
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
            implicit_feedback_max_id=implicit_feedback_max_id,
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
            implicit_feedback_max_id=implicit_feedback_max_id,
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
        implicit_feedback_max_id=implicit_feedback_max_id,
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
    allow_implicit: bool = False,
    implicit_min_confirmations: int = 2,
    implicit_max_generic_queries: int = 5,
) -> RouteDecision:
    """Read and apply one route while retaining the route snapshot high-water."""

    route: FeedbackRoute | None = None
    feedback_max_id: int | None = None
    implicit_feedback_max_id: int | None = None
    negative_feedback: tuple[NegativeFeedback, ...] = ()
    if usage_db_path.is_file():
        try:
            with _connect_readonly(usage_db_path) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN")
                route, feedback_max_id, negative_feedback = _feedback_route_snapshot(
                    usage_db_path,
                    root=root,
                    query=query,
                    artifact_type=artifact_type,
                    connection=connection,
                )
                if _has_table(connection, "implicit_feedback") and {
                    "id",
                    "root",
                }.issubset(_table_columns(connection, "implicit_feedback")):
                    implicit_high_water_row = connection.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM implicit_feedback WHERE root = ?",
                        (str(root),),
                    ).fetchone()
                    implicit_feedback_max_id = _persisted_id(
                        implicit_high_water_row[0], allow_zero=True
                    )
                if route is None and allow_implicit:
                    route, implicit_feedback_max_id = _implicit_feedback_route_snapshot(
                        usage_db_path,
                        root=root,
                        query=query,
                        artifact_type=artifact_type,
                        min_confirmations=implicit_min_confirmations,
                        max_generic_queries=implicit_max_generic_queries,
                        connection=connection,
                    )
        except (OSError, sqlite3.Error):
            route = None
            feedback_max_id = None
            implicit_feedback_max_id = None
            negative_feedback = ()
    if route is not None and route.feedback_id is None and any(
        not rejection.artifact_id or rejection.artifact_id == route.artifact_id
        for rejection in negative_feedback
    ):
        route = None
    return apply_feedback_route(
        rows,
        route=route,
        feedback_max_id=feedback_max_id,
        db_path=db_path,
        limit=limit,
        search_index_fn=search_index_fn,
        implicit_feedback_max_id=implicit_feedback_max_id,
    )
