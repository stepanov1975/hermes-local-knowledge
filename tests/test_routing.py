from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_local_knowledge.routing import (
    FeedbackRoute,
    RouteOutcome,
    _parsed_utc_timestamp,
    apply_feedback_route,
    best_feedback_route,
    decide_feedback_route,
)
from hermes_local_knowledge.telemetry import _record_feedback


def _feedback(
    usage_db_path: Path,
    root: Path,
    *,
    rating: str,
    query: str,
    artifact_id: str,
) -> None:
    _record_feedback(
        root,
        rating=rating,
        event_id=None,
        query=query,
        artifact_id=artifact_id,
        note="routing test",
        context={},
        usage_db_path=usage_db_path,
    )


def test_parsed_utc_timestamp_rejects_conversion_overflow() -> None:
    assert _parsed_utc_timestamp("9999-12-31T23:59:59-23:59") is None


def test_route_maps_nonmatching_id_prefix_and_requires_a_concise_retry(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _feedback(
        usage_db_path,
        root,
        rating="useful",
        query="paperless document retrieval",
        artifact_id="mcp:paperless",
    )

    route = best_feedback_route(
        usage_db_path,
        root=root,
        query="paperless document retrieval help",
        artifact_type=None,
    )
    non_concise = best_feedback_route(
        usage_db_path,
        root=root,
        query="paperless document",
        artifact_type=None,
    )

    assert route is not None
    assert route.artifact_type == "mcp_server"
    assert non_concise is None


def test_route_length_counts_repeated_query_tokens(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _feedback(
        usage_db_path,
        root,
        rating="useful",
        query="find find find docker image version report",
        artifact_id="runbook:accepted",
    )

    assert best_feedback_route(
        usage_db_path,
        root=root,
        query="find docker image version report",
        artifact_type=None,
    ) is None


def test_feedback_schema_has_a_bounded_route_lookup_index(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _feedback(
        usage_db_path,
        root,
        rating="useful",
        query="docker image version report",
        artifact_id="runbook:target",
    )

    with sqlite3.connect(usage_db_path) as connection:
        index_names = {
            str(row[1])
            for row in connection.execute("PRAGMA index_list(feedback)")
        }

    assert "idx_feedback_root_id" in index_names


def test_feedback_lookup_fails_open_quickly_when_the_database_is_locked(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _feedback(
        usage_db_path,
        root,
        rating="useful",
        query="docker image version report",
        artifact_id="runbook:target",
    )

    locker = sqlite3.connect(usage_db_path)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        route = best_feedback_route(
            usage_db_path,
            root=root,
            query="docker image version report",
            artifact_type=None,
        )
        elapsed = time.monotonic() - started
    finally:
        locker.rollback()
        locker.close()

    assert route is None
    assert elapsed < 1.0


@pytest.mark.parametrize("rejected_artifact_id", ["runbook:target", ""])
def test_newer_current_query_rejection_suppresses_an_older_overlap_route(
    tmp_path: Path,
    rejected_artifact_id: str,
) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _feedback(
        usage_db_path,
        root,
        rating="useful",
        query="docker image version report",
        artifact_id="runbook:target",
    )
    _feedback(
        usage_db_path,
        root,
        rating="wrong_artifact",
        query="docker image version report needs update",
        artifact_id=rejected_artifact_id,
    )

    route = best_feedback_route(
        usage_db_path,
        root=root,
        query="docker image version report needs update",
        artifact_type=None,
    )

    assert route is None


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(
            "?",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="question marks are not valid Windows path characters",
            ),
        ),
        "#",
    ],
)
def test_readonly_feedback_lookup_handles_uri_reserved_path_characters(
    tmp_path: Path,
    marker: str,
) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / f"state{marker}suffix" / "usage.sqlite"
    _feedback(
        usage_db_path,
        root,
        rating="useful",
        query="docker image version report",
        artifact_id="runbook:target",
    )

    route = best_feedback_route(
        usage_db_path,
        root=root,
        query="docker image version report",
        artifact_type=None,
    )

    assert route is not None
    assert route.artifact_id == "runbook:target"


def test_route_outcome_values_match_the_persisted_contract() -> None:
    assert [outcome.value for outcome in RouteOutcome] == [
        "none",
        "already_first",
        "promoted_existing",
        "promoted_retry",
        "verification_failed",
    ]


@pytest.mark.parametrize(
    ("baseline", "retry_rows", "expected_ids", "expected_outcome"),
    [
        (
            [{"id": "runbook:target"}, {"id": "runbook:other"}],
            [],
            ["runbook:target", "runbook:other"],
            RouteOutcome.ALREADY_FIRST,
        ),
        (
            [{"id": "runbook:other"}, {"id": "runbook:target"}],
            [],
            ["runbook:target", "runbook:other"],
            RouteOutcome.PROMOTED_EXISTING,
        ),
        (
            [{"id": "runbook:other"}],
            [{"id": "runbook:target"}],
            ["runbook:target", "runbook:other"],
            RouteOutcome.PROMOTED_RETRY,
        ),
        (
            [{"id": "runbook:other"}],
            [{"id": "runbook:still-other"}],
            ["runbook:other"],
            RouteOutcome.VERIFICATION_FAILED,
        ),
    ],
)
def test_apply_feedback_route_returns_typed_provenance_for_each_matched_outcome(
    tmp_path: Path,
    baseline: list[dict[str, str]],
    retry_rows: list[dict[str, str]],
    expected_ids: list[str],
    expected_outcome: RouteOutcome,
) -> None:
    route = FeedbackRoute(
        query="accepted concise query",
        artifact_id="runbook:target",
        artifact_type="runbook",
        terms=frozenset({"accepted", "concise", "query"}),
        feedback_id=17,
    )

    decision = apply_feedback_route(
        baseline,
        route=route,
        feedback_max_id=23,
        db_path=tmp_path / "index.sqlite",
        limit=3,
        search_index_fn=lambda *_args, **_kwargs: retry_rows,
    )

    assert [str(row["id"]) for row in decision.rows] == expected_ids
    assert decision.outcome is expected_outcome
    assert decision.feedback_id == 17
    assert decision.artifact_id == "runbook:target"
    assert decision.feedback_max_id == 23


def test_no_route_lookup_returns_typed_decision_and_advances_irrelevant_high_water(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _feedback(
        usage_db_path,
        root,
        rating="other",
        query="irrelevant neutral feedback",
        artifact_id="runbook:irrelevant",
    )
    baseline = [{"id": "runbook:baseline", "type": "runbook"}]

    decision = decide_feedback_route(
        baseline,
        usage_db_path=usage_db_path,
        root=root,
        query="today matcher query",
        artifact_type=None,
        db_path=tmp_path / "index.sqlite",
        limit=3,
        search_index_fn=lambda *_args, **_kwargs: pytest.fail("no retry expected"),
    )

    assert decision.rows is baseline
    assert decision.outcome is RouteOutcome.NONE
    assert decision.feedback_id is None
    assert decision.artifact_id is None
    assert decision.feedback_max_id == 1


def test_unavailable_feedback_database_returns_typed_none_with_null_high_water(
    tmp_path: Path,
) -> None:
    baseline = [{"id": "runbook:baseline", "type": "runbook"}]

    decision = decide_feedback_route(
        baseline,
        usage_db_path=tmp_path / "missing" / "usage.sqlite",
        root=tmp_path / "root",
        query="today matcher query",
        artifact_type=None,
        db_path=tmp_path / "index.sqlite",
        limit=3,
        search_index_fn=lambda *_args, **_kwargs: pytest.fail("no retry expected"),
    )

    assert decision.rows is baseline
    assert decision.outcome is RouteOutcome.NONE
    assert decision.feedback_max_id is None
