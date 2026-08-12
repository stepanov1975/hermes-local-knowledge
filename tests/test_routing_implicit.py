from __future__ import annotations

import json
from pathlib import Path

from hermes_local_knowledge.routing import best_feedback_route
from hermes_local_knowledge.telemetry import _usage_connect, record_implicit_feedback


def _implicit_confirm(
    usage_db_path: Path,
    root: Path,
    *,
    session_id: str,
    query: str,
    artifact_id: str,
) -> int:
    """Seed one verified implicit confirmation for a query/artifact pair."""

    connection = _usage_connect(root, usage_db_path, initialize=True)
    try:
        cursor = connection.execute(
            """
            INSERT INTO usage_events (
                ts, tool, client, session_id, query, top_ids_json, success, root
            ) VALUES (?, 'knowledge_search', 'test', ?, ?, ?, 1, ?)
            """,
            (
                "2026-08-11T00:00:00Z",
                session_id,
                query,
                json.dumps([artifact_id], ensure_ascii=False),
                str(root),
            ),
        )
        connection.commit()
        search_event_id = cursor.lastrowid
        assert search_event_id is not None
    finally:
        connection.close()
    feedback_id = record_implicit_feedback(
        root,
        search_event_id=int(search_event_id),
        query=query,
        artifact_id=artifact_id,
        context={},
        usage_db_path=usage_db_path,
    )
    assert feedback_id is not None
    return feedback_id


def test_implicit_route_requires_min_confirmations(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _implicit_confirm(
        usage_db_path,
        root,
        session_id="s-1",
        query="docker image version report",
        artifact_id="runbook:target",
    )

    assert (
        best_feedback_route(
            usage_db_path,
            root=root,
            query="docker image version report",
            artifact_type=None,
        )
        is None
    )

    _implicit_confirm(
        usage_db_path,
        root,
        session_id="s-2",
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


def test_implicit_min_confirmations_is_configurable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _implicit_confirm(
        usage_db_path,
        root,
        session_id="s-1",
        query="docker image version report",
        artifact_id="runbook:target",
    )

    route = best_feedback_route(
        usage_db_path,
        root=root,
        query="docker image version report",
        artifact_type=None,
        min_confirmations=1,
    )

    assert route is not None
    assert route.artifact_id == "runbook:target"


def test_explicit_route_wins_over_implicit_route(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _implicit_confirm(
        usage_db_path,
        root,
        session_id="s-1",
        query="docker image version report",
        artifact_id="runbook:implicit",
    )
    _implicit_confirm(
        usage_db_path,
        root,
        session_id="s-2",
        query="docker image version report",
        artifact_id="runbook:implicit",
    )
    from hermes_local_knowledge.telemetry import _record_feedback

    _record_feedback(
        root,
        rating="useful",
        event_id=None,
        query="docker image version report",
        artifact_id="runbook:explicit",
        note="routing test",
        context={},
        usage_db_path=usage_db_path,
    )

    route = best_feedback_route(
        usage_db_path,
        root=root,
        query="docker image version report",
        artifact_type=None,
    )

    assert route is not None
    assert route.artifact_id == "runbook:explicit"


def test_newer_explicit_rejection_suppresses_implicit_route(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _implicit_confirm(
        usage_db_path,
        root,
        session_id="s-1",
        query="docker image version report",
        artifact_id="runbook:target",
    )
    _implicit_confirm(
        usage_db_path,
        root,
        session_id="s-2",
        query="docker image version report",
        artifact_id="runbook:target",
    )
    from hermes_local_knowledge.telemetry import _record_feedback

    _record_feedback(
        root,
        rating="wrong_artifact",
        event_id=None,
        query="docker image version report needs update",
        artifact_id="",
        note="routing test",
        context={},
        usage_db_path=usage_db_path,
    )

    assert (
        best_feedback_route(
            usage_db_path,
            root=root,
            query="docker image version report needs update",
            artifact_type=None,
        )
        is None
    )


def test_generic_artifact_implicit_rows_are_ignored(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    queries = [
        "alpha one two",
        "beta three four",
        "gamma five six",
        "delta seven eight",
        "epsilon nine ten",
        "zeta eleven twelve",
    ]
    for index, query in enumerate(queries):
        for session in range(2):
            _implicit_confirm(
                usage_db_path,
                root,
                session_id=f"generic-{index}-{session}",
                query=query,
                artifact_id="runbook:generic",
            )

    assert (
        best_feedback_route(
            usage_db_path,
            root=root,
            query="alpha one two",
            artifact_type=None,
        )
        is None
    )

    route = best_feedback_route(
        usage_db_path,
        root=root,
        query="alpha one two",
        artifact_type=None,
        max_generic_queries=10,
    )
    assert route is not None
    assert route.artifact_id == "runbook:generic"


def test_implicit_route_respects_artifact_type_filter(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    _implicit_confirm(
        usage_db_path,
        root,
        session_id="s-1",
        query="docker image version report",
        artifact_id="runbook:target",
    )
    _implicit_confirm(
        usage_db_path,
        root,
        session_id="s-2",
        query="docker image version report",
        artifact_id="runbook:target",
    )

    assert (
        best_feedback_route(
            usage_db_path,
            root=root,
            query="docker image version report",
            artifact_type="mcp_server",
        )
        is None
    )
    route = best_feedback_route(
        usage_db_path,
        root=root,
        query="docker image version report",
        artifact_type="runbook",
    )
    assert route is not None
    assert route.artifact_id == "runbook:target"


def test_legacy_schema_without_origin_column_still_routes_explicit_feedback(
    tmp_path: Path,
) -> None:
    """Regression: a database created before the ``origin`` column was added
    must still route explicit feedback (Codex review P2, PR #27).

    On upgraded profiles the schema migration runs on the next telemetry
    write, so the first managed search after an upgrade hits the old schema.
    Selecting ``f.origin`` there raised ``no such column``, which the broad
    ``sqlite3.Error`` path swallowed and turned into "no route" — silently
    dropping every existing explicit feedback route.
    """

    import sqlite3

    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    usage_db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(usage_db_path)
    try:
        # Minimal legacy schema: feedback table WITHOUT the origin column,
        # plus the usage_events table the snapshot query left-joins to.
        connection.execute(
            """
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT '',
                root TEXT NOT NULL,
                query TEXT NOT NULL DEFAULT '',
                rating TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                event_id INTEGER,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT '',
                tool TEXT NOT NULL DEFAULT '',
                client TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                top_ids_json TEXT NOT NULL DEFAULT '[]',
                success INTEGER NOT NULL DEFAULT 0,
                root TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            INSERT INTO feedback (root, query, rating, artifact_id)
            VALUES (?, 'docker image version report', 'useful', 'runbook:legacy')
            """,
            (str(root),),
        )
        connection.commit()
    finally:
        connection.close()

    route = best_feedback_route(
        usage_db_path,
        root=root,
        query="docker image version report",
        artifact_type=None,
    )

    assert route is not None
    assert route.artifact_id == "runbook:legacy"
