from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_local_knowledge.routing import best_feedback_route
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


@pytest.mark.parametrize("marker", ["?", "#"])
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
