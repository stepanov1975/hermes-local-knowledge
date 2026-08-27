from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_local_knowledge import __version__
from hermes_local_knowledge import telemetry


def _event(
    root: Path,
    db_path: Path,
    *,
    tool: str = "knowledge_search",
    success: bool = True,
    query: str = "  exact   replay query  ",
    ids: list[str] | None = None,
) -> int:
    event_id = telemetry._record_usage(
        root,
        tool=tool,
        success=success,
        query=query,
        artifact_id="skill:returned" if tool != "knowledge_search" else "",
        top_ids=ids or ["skill:returned"],
        top_types=["skill"] * len(ids or ["skill:returned"]),
        usage_db_path=db_path,
    )
    assert event_id is not None
    return event_id


def _feedback(
    root: Path,
    db_path: Path,
    *,
    rating: str = "useful",
    event_id: int | None = None,
    query: str = "",
    artifact_id: str = "",
    note: str = "",
    expected_artifact_id: str = "",
    resolves_feedback_id: int | None = None,
    existing: set[str] | None = None,
) -> tuple[int, int]:
    return telemetry._record_feedback(
        root,
        rating=rating,
        event_id=event_id,
        query=query,
        artifact_id=artifact_id,
        note=note,
        expected_artifact_id=expected_artifact_id,
        resolves_feedback_id=resolves_feedback_id,
        artifact_exists=lambda value: value in (existing or set()),
        context={},
        usage_db_path=db_path,
    )


def test_event_link_canonicalizes_exact_query_and_writes_feedback_usage_atomically(tmp_path: Path) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    query = "  exact\x00   replay\tquery  "
    event_id = _event(root, db_path, query=query, ids=["skill:first", "skill:last"])

    feedback_id, usage_id = _feedback(
        root,
        db_path,
        rating="wrong_artifact",
        event_id=event_id,
        query="exact\x00   replay\tquery",
        artifact_id="skill:last",
        note="  keep\nthis exact note  ",
    )

    with sqlite3.connect(db_path) as conn:
        feedback = conn.execute(
            "SELECT query, artifact_id, note, linkage_status FROM feedback WHERE id=?",
            (feedback_id,),
        ).fetchone()
        usage = conn.execute(
            "SELECT tool, success, query, artifact_id FROM usage_events WHERE id=?",
            (usage_id,),
        ).fetchone()
    assert feedback == (
        "exact\x00   replay\tquery",
        "skill:last",
        "  keep\nthis exact note  ",
        "verified_event",
    )
    assert usage == (
        "knowledge_feedback",
        1,
        "exact\x00   replay\tquery",
        "skill:last",
    )


def test_feedback_initialization_and_rows_share_one_writer_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    statements: list[str] = []
    initialize_values: list[bool] = []
    original_connect = telemetry._usage_connect

    def traced_connect(
        root_arg: Path | None,
        usage_db_arg: Path | None = None,
        *,
        initialize: bool = True,
    ) -> sqlite3.Connection:
        initialize_values.append(initialize)
        conn = original_connect(root_arg, usage_db_arg, initialize=initialize)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(telemetry, "_usage_connect", traced_connect)

    _feedback(root, db_path, rating="other", query="direct query")

    normalized = [statement.strip().upper() for statement in statements]
    begin_index = normalized.index("BEGIN IMMEDIATE")
    create_index = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("CREATE TABLE IF NOT EXISTS USAGE_EVENTS")
    )
    feedback_index = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("INSERT INTO FEEDBACK")
    )
    usage_index = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("INSERT INTO USAGE_EVENTS")
    )
    commit_index = normalized.index("COMMIT")
    assert initialize_values == [False]
    assert begin_index < create_index < feedback_index < usage_index < commit_index


@pytest.mark.parametrize(
    ("event_kind", "message"),
    [
        ("missing", "usage event does not exist"),
        ("cross_root", "current root"),
        ("non_lookup", "supported lookup tool"),
        ("failed", "successful lookup"),
        ("query_conflict", "does not match"),
        ("artifact_absent", "recorded result page"),
    ],
)
def test_invalid_event_links_are_rejected_without_feedback_rows(
    tmp_path: Path, event_kind: str, message: str
) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    event_root = tmp_path / "other" if event_kind == "cross_root" else root
    tool = "knowledge_usage_report" if event_kind == "non_lookup" else "knowledge_search"
    event_id = _event(
        event_root,
        db_path,
        tool=tool,
        success=event_kind != "failed",
        query="canonical query",
        ids=["skill:returned"],
    )
    if event_kind == "missing":
        event_id += 1000
    query = "conflicting query" if event_kind == "query_conflict" else "canonical query"
    artifact_id = "skill:absent" if event_kind == "artifact_absent" else "skill:returned"

    with pytest.raises(ValueError, match=message):
        _feedback(
            root,
            db_path,
            rating="wrong_artifact",
            event_id=event_id,
            query=query,
            artifact_id=artifact_id,
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM usage_events WHERE tool='knowledge_feedback'").fetchone() == (0,)


@pytest.mark.parametrize(
    ("tool", "artifact_id", "message"),
    [
        ("knowledge_get", "skill:other", "referenced artifact lookup"),
        ("knowledge_neighbors", "skill:other", "referenced neighbor lookup"),
    ],
)
def test_nonsearch_event_artifacts_must_match_recorded_results(
    tmp_path: Path,
    tool: str,
    artifact_id: str,
    message: str,
) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    event_id = _event(root, db_path, tool=tool, query="", ids=["skill:neighbor"])

    with pytest.raises(ValueError, match=message):
        _feedback(
            root,
            db_path,
            rating="wrong_artifact",
            event_id=event_id,
            artifact_id=artifact_id,
        )


def test_neighbor_feedback_accepts_a_recorded_neighbor_result(tmp_path: Path) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    event_id = _event(
        root,
        db_path,
        tool="knowledge_neighbors",
        query="",
        ids=["skill:neighbor"],
    )

    feedback_id, _ = _feedback(
        root,
        db_path,
        rating="useful",
        event_id=event_id,
        artifact_id="skill:neighbor",
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT artifact_id, linkage_status FROM feedback WHERE id=?",
            (feedback_id,),
        ).fetchone()
    assert row == ("skill:neighbor", "verified_event")


@pytest.mark.parametrize(
    ("query", "artifact_id", "note", "status"),
    [
        ("direct query", "", "", "direct_query"),
        ("", "skill:artifact", "", "artifact_only"),
        ("", "", "", "unscoped"),
        ("", "", "note only", "unscoped"),
    ],
)
def test_existing_direct_and_unscoped_feedback_calls_remain_supported(
    tmp_path: Path, query: str, artifact_id: str, note: str, status: str
) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    feedback_id, _usage_id = _feedback(
        root,
        db_path,
        rating="other",
        query=query,
        artifact_id=artifact_id,
        note=note,
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT linkage_status FROM feedback WHERE id=?", (feedback_id,)).fetchone()
    assert row == (status,)


@pytest.mark.parametrize("rating", ["useful", "other"])
def test_expected_target_requires_negative_search_feedback(tmp_path: Path, rating: str) -> None:
    with pytest.raises(ValueError, match="negative search-quality rating"):
        _feedback(
            tmp_path / "root",
            tmp_path / "usage.sqlite",
            rating=rating,
            query="direct query",
            expected_artifact_id="skill:expected",
            existing={"skill:expected"},
        )


def test_expected_target_must_exist_and_resolution_is_verified_and_unique(tmp_path: Path) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    rejected_event = _event(root, db_path, query="bad route", ids=["skill:rejected"])
    with pytest.raises(ValueError, match="current managed index"):
        _feedback(
            root,
            db_path,
            rating="wrong_artifact",
            event_id=rejected_event,
            artifact_id="skill:rejected",
            expected_artifact_id="skill:accepted",
        )
    negative_id, _ = _feedback(
        root,
        db_path,
        rating="wrong_artifact",
        event_id=rejected_event,
        artifact_id="skill:rejected",
        expected_artifact_id="skill:accepted",
        existing={"skill:accepted"},
    )
    accepted_event = _event(root, db_path, query="accepted route", ids=["skill:noise", "skill:accepted"])
    resolution_id, _ = _feedback(
        root,
        db_path,
        rating="useful",
        event_id=accepted_event,
        artifact_id="skill:accepted",
        resolves_feedback_id=negative_id,
        existing={"skill:accepted"},
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT resolves_feedback_id, linkage_status FROM feedback WHERE id=?", (resolution_id,)
        ).fetchone()
        before = conn.execute("SELECT COUNT(*) FROM usage_events WHERE tool='knowledge_feedback'").fetchone()
    assert row == (negative_id, "verified_event")

    with pytest.raises(ValueError, match="already explicitly resolved"):
        _feedback(
            root,
            db_path,
            rating="useful",
            event_id=accepted_event,
            artifact_id="skill:accepted",
            resolves_feedback_id=negative_id,
            existing={"skill:accepted"},
        )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events WHERE tool='knowledge_feedback'").fetchone() == before


@pytest.mark.parametrize("parent_linkage", ["direct_query", "legacy"])
def test_resolution_accepts_replayable_direct_and_migrated_negative_parents(
    tmp_path: Path,
    parent_linkage: str,
) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    negative_id, _ = _feedback(
        root,
        db_path,
        rating="wrong_artifact",
        query="bad route",
        artifact_id="skill:rejected",
    )
    if parent_linkage == "legacy":
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE feedback SET linkage_status='legacy' WHERE id=?",
                (negative_id,),
            )
    accepted_event = _event(
        root,
        db_path,
        query="accepted route",
        ids=["skill:accepted"],
    )

    resolution_id, _ = _feedback(
        root,
        db_path,
        rating="useful",
        event_id=accepted_event,
        artifact_id="skill:accepted",
        resolves_feedback_id=negative_id,
        existing={"skill:accepted"},
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT resolves_feedback_id FROM feedback WHERE id=?",
            (resolution_id,),
        ).fetchone()
    assert row == (negative_id,)


@pytest.mark.parametrize(
    "parent_kind",
    ["artifact_lookup", "artifact_only", "legacy_unscoped"],
)
def test_resolution_rejects_negative_parents_without_replayable_search_intent(
    tmp_path: Path,
    parent_kind: str,
) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    if parent_kind == "artifact_lookup":
        rejected_event = _event(
            root,
            db_path,
            tool="knowledge_get",
            query="",
            ids=[],
        )
        negative_id, _ = _feedback(
            root,
            db_path,
            rating="wrong_artifact",
            event_id=rejected_event,
            artifact_id="skill:returned",
        )
    else:
        negative_id, _ = _feedback(
            root,
            db_path,
            rating="wrong_artifact",
            artifact_id="skill:rejected" if parent_kind == "artifact_only" else "",
        )
        if parent_kind == "legacy_unscoped":
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE feedback SET linkage_status='legacy' WHERE id=?",
                    (negative_id,),
                )
    accepted_event = _event(
        root,
        db_path,
        query="accepted route",
        ids=["skill:accepted"],
    )

    with pytest.raises(ValueError, match="no replayable search intent"):
        _feedback(
            root,
            db_path,
            rating="useful",
            event_id=accepted_event,
            artifact_id="skill:accepted",
            resolves_feedback_id=negative_id,
            existing={"skill:accepted"},
        )


def test_resolution_can_verify_target_when_parent_did_not_predict_one(tmp_path: Path) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    rejected_event = _event(root, db_path, query="bad route", ids=["skill:rejected"])
    negative_id, _ = _feedback(
        root,
        db_path,
        rating="wrong_artifact",
        event_id=rejected_event,
        artifact_id="skill:rejected",
    )
    accepted_event = _event(root, db_path, query="accepted route", ids=["skill:accepted"])

    resolution_id, _ = _feedback(
        root,
        db_path,
        rating="useful",
        event_id=accepted_event,
        artifact_id="skill:accepted",
        resolves_feedback_id=negative_id,
        existing={"skill:accepted"},
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT artifact_id, resolves_feedback_id, linkage_status FROM feedback WHERE id=?",
            (resolution_id,),
        ).fetchone()
    assert row == ("skill:accepted", negative_id, "verified_event")


def test_resolution_rejects_expected_target_disagreement(tmp_path: Path) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    rejected_event = _event(root, db_path, query="bad route", ids=["skill:rejected"])
    negative_id, _ = _feedback(
        root,
        db_path,
        rating="wrong_artifact",
        event_id=rejected_event,
        artifact_id="skill:rejected",
        expected_artifact_id="skill:expected",
        existing={"skill:expected"},
    )
    accepted_event = _event(root, db_path, query="accepted route", ids=["skill:other"])
    with pytest.raises(ValueError, match="expected artifact"):
        _feedback(
            root,
            db_path,
            rating="useful",
            event_id=accepted_event,
            artifact_id="skill:other",
            resolves_feedback_id=negative_id,
            existing={"skill:other"},
        )


def test_concurrent_additive_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")

    barrier = threading.Barrier(5)
    errors: list[str] = []
    error_lock = threading.Lock()

    def migrate() -> None:
        try:
            with sqlite3.connect(db_path, timeout=1.0) as conn:
                barrier.wait()
                telemetry._ensure_columns(
                    conn,
                    "sample",
                    {"first_value": "TEXT", "second_value": "INTEGER DEFAULT -1"},
                )
        except Exception as exc:  # pragma: no cover - assertion reports concurrent details
            with error_lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=migrate) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with sqlite3.connect(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sample)")}
    assert errors == []
    assert {"id", "first_value", "second_value"} <= columns


def test_v042_schema_migrates_additively_and_rollback_writer_gets_legacy_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite"
    root = tmp_path / "root"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, tool TEXT NOT NULL,
                client TEXT NOT NULL DEFAULT 'native', session_id TEXT, task_id TEXT, tool_call_id TEXT,
                query TEXT, artifact_id TEXT, artifact_type TEXT, limit_value INTEGER,
                rebuild_requested INTEGER NOT NULL DEFAULT 0, rebuilt INTEGER, success INTEGER NOT NULL,
                error TEXT, result_count INTEGER, top_ids_json TEXT NOT NULL DEFAULT '[]',
                top_types_json TEXT NOT NULL DEFAULT '[]', latency_ms INTEGER, plugin_version TEXT,
                source_root_source TEXT, state_dir_source TEXT, include_markdown_docs_source TEXT,
                index_exists INTEGER, index_mtime TEXT, index_age_seconds INTEGER,
                index_artifact_count INTEGER, index_edge_count INTEGER,
                index_artifact_counts_json TEXT NOT NULL DEFAULT '{}', index_metadata_error TEXT,
                build_duration_ms INTEGER, root TEXT, db_path TEXT
            );
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event_id INTEGER,
                rating TEXT NOT NULL, query TEXT, artifact_id TEXT, note TEXT,
                session_id TEXT, task_id TEXT, tool_call_id TEXT, root TEXT
            );
            INSERT INTO usage_events (ts, tool, success, root) VALUES ('before', 'knowledge_search', 1, 'legacy');
            INSERT INTO feedback (ts, rating, root) VALUES ('before', 'other', 'legacy');
            """
        )

    telemetry._record_usage(root, tool="knowledge_search", success=True, usage_db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        usage_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(usage_events)")}
        feedback_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(feedback)")}
        assert {
            "turn_id", "baseline_top_ids_json", "route_feedback_id", "route_artifact_id", "route_outcome",
            "index_jsonl_sha256", "index_format_version", "feedback_max_id",
        } <= usage_columns
        assert {"expected_artifact_id", "resolves_feedback_id", "linkage_status"} <= feedback_columns
        assert conn.execute(
            "SELECT turn_id, feedback_max_id, baseline_top_ids_json, route_outcome "
            "FROM usage_events WHERE id=1"
        ).fetchone() == (None, -1, "[]", "none")
        assert conn.execute("SELECT linkage_status FROM feedback WHERE id=1").fetchone() == ("legacy",)
        indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list(feedback)")}
        assert {"idx_feedback_resolution_unique", "idx_feedback_route_lookup"} <= indexes
        usage_indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list(usage_events)")}
        assert "idx_usage_events_implicit_lookup" in usage_indexes
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM usage_events "
            "WHERE session_id=? AND task_id=? AND turn_id=? AND root=? "
            "AND tool='knowledge_search' AND success=1 ORDER BY id DESC LIMIT 20",
            ("s", "t", "turn", "root"),
        ).fetchall()
        assert any("idx_usage_events_implicit_lookup" in str(row[3]) for row in plan)
        # Simulate a rolled-back v0.4.2 writer that knows none of the additive columns.
        conn.execute(
            "INSERT INTO usage_events (ts, tool, success, root) VALUES ('rollback', 'knowledge_search', 1, ?)",
            (str(root),),
        )
        rollback = conn.execute(
            "SELECT turn_id, feedback_max_id, baseline_top_ids_json, route_outcome "
            "FROM usage_events WHERE ts='rollback'"
        ).fetchone()
    assert rollback == (None, -1, "[]", "none")


def test_usage_report_classifies_linkage_explicit_resolution_and_candidate_quality(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    other_root = tmp_path / "other"
    db_path = tmp_path / "usage.sqlite"
    root.mkdir()
    other_root.mkdir()

    negative_event = _event(
        root,
        db_path,
        query="find correct runbook",
        ids=["skill:wrong"],
    )
    negative_id, _ = telemetry._record_feedback(
        root,
        rating="wrong_artifact",
        event_id=negative_event,
        query="find correct runbook",
        artifact_id="skill:wrong",
        expected_artifact_id="skill:target",
        note="wrong result",
        context={},
        artifact_exists=lambda artifact_id: artifact_id == "skill:target",
        usage_db_path=db_path,
    )
    ordinary_positive_event = _event(
        root,
        db_path,
        query="find correct runbook",
        ids=["skill:target"],
    )
    telemetry._record_feedback(
        root,
        rating="useful",
        event_id=ordinary_positive_event,
        query="find correct runbook",
        artifact_id="skill:target",
        note="ordinary positive must not resolve a verified negative",
        context={},
        usage_db_path=db_path,
    )
    unresolved_report = telemetry._usage_report(root, days=30, limit=20, usage_db_path=db_path)
    assert [row["id"] for row in unresolved_report["unresolved_negative_feedback"]] == [
        negative_id
    ]

    resolution_event = _event(
        root,
        db_path,
        query="find correct runbook",
        ids=["skill:target"],
    )
    resolution_id, _ = telemetry._record_feedback(
        root,
        rating="useful",
        event_id=resolution_event,
        query="find correct runbook",
        artifact_id="skill:target",
        note="accepted correction",
        context={},
        resolves_feedback_id=negative_id,
        artifact_exists=lambda artifact_id: artifact_id == "skill:target",
        usage_db_path=db_path,
    )
    direct_id, _ = telemetry._record_feedback(
        root,
        rating="missing",
        event_id=None,
        query="missing capability",
        artifact_id="",
        note="needs coverage triage",
        context={},
        usage_db_path=db_path,
    )
    telemetry._record_feedback(
        root,
        rating="noisy",
        event_id=None,
        query="XXXX",
        artifact_id="",
        note="probe noise",
        context={},
        usage_db_path=db_path,
    )
    telemetry._record_feedback(
        root,
        rating="not_useful",
        event_id=None,
        query="",
        artifact_id="skill:wrong",
        note="artifact-only",
        context={},
        usage_db_path=db_path,
    )
    other_event = _event(other_root, db_path, query="cross root", ids=["skill:wrong"])
    failed_event = _event(root, db_path, query="failed lookup", success=False)
    now = telemetry._utc_now()
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO feedback (
                ts, rating, event_id, root, query, artifact_id, note,
                linkage_status
            ) VALUES (?, 'missing', ?, ?, ?, '', '', 'verified_event')
            """,
            [
                (now, 999_999, str(root), "orphaned lookup"),
                (now, other_event, str(root), "cross root"),
                (now, failed_event, str(root), "failed lookup"),
            ],
        )

    report = telemetry._usage_report(root, days=30, limit=20, usage_db_path=db_path)

    assert report["explicit_resolutions"] == [
        {
            "feedback_id": negative_id,
            "resolution_feedback_id": resolution_id,
            "resolved_at": report["explicit_resolutions"][0]["resolved_at"],
            "accepted_artifact_id": "skill:target",
            "accepted_query": "find correct runbook",
        }
    ]
    assert report["resolved_negative_feedback"][0]["resolution_kind"] == "explicit"
    assert [row["id"] for row in report["search_issue_candidates"]] == [direct_id]
    assert report["feedback_linkage_counts"]["orphaned_event"] == 1
    assert report["feedback_linkage_counts"]["root_mismatch"] == 1
    assert report["feedback_linkage_counts"]["artifact_only"] == 1
    assert sum(report["feedback_linkage_counts"].values()) == report["feedback_count"]
    assert sum(report["feedback_resolution_counts"].values()) == report["feedback_count"]
    assert report["feedback_resolution_counts"]["explicit_resolution"] == 1
    assert report["feedback_resolution_counts"]["explicitly_resolved_negative"] == 1
    assert report["replay_ready_label_counts"]["explicit_resolution"] == 1
    assert all(row["effective_query"] != "failed lookup" for row in report["search_issue_candidates"])


def test_usage_report_rejects_malformed_verified_feedback_as_a_correction_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    db_path = tmp_path / "usage.sqlite"
    root.mkdir()
    event_id = _event(root, db_path, query="claimed search", ids=["skill:shown"])
    now = telemetry._utc_now()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO feedback (
                ts, rating, event_id, root, query, artifact_id, note,
                expected_artifact_id, linkage_status
            ) VALUES (?, 'wrong_artifact', ?, ?, ?, ?, '', ?, 'verified_event')
            """,
            (
                now,
                event_id,
                str(root),
                "claimed search",
                "skill:not-shown",
                "skill:target",
            ),
        )
        feedback_id = int(cursor.lastrowid or 0)

    report = telemetry._usage_report(root, days=30, limit=20, usage_db_path=db_path)

    assert report["feedback_linkage_counts"].get("verified_event", 0) == 0
    assert feedback_id not in {row["id"] for row in report["search_issue_candidates"]}


def test_usage_report_rejects_resolution_when_target_was_not_returned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    db_path = tmp_path / "usage.sqlite"
    root.mkdir()
    negative_event = _event(root, db_path, query="broken route", ids=["skill:wrong"])
    negative_id, _ = telemetry._record_feedback(
        root,
        rating="wrong_artifact",
        event_id=negative_event,
        query="broken route",
        artifact_id="skill:wrong",
        expected_artifact_id="skill:target",
        note="wrong result",
        context={},
        artifact_exists=lambda artifact_id: artifact_id == "skill:target",
        usage_db_path=db_path,
    )
    resolution_event = _event(root, db_path, query="correct route", ids=["skill:target"])
    telemetry._record_feedback(
        root,
        rating="useful",
        event_id=resolution_event,
        query="correct route",
        artifact_id="skill:target",
        note="accepted target",
        context={},
        resolves_feedback_id=negative_id,
        artifact_exists=lambda artifact_id: artifact_id == "skill:target",
        usage_db_path=db_path,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE usage_events SET top_ids_json = ? WHERE id = ?",
            ('["skill:other"]', resolution_event),
        )

    report = telemetry._usage_report(root, days=30, limit=20, usage_db_path=db_path)

    assert report["explicit_resolutions"] == []
    assert report["explicit_resolution_count"] == 0
    assert report["replay_ready_label_counts"]["explicit_resolution"] == 0
    assert [row["id"] for row in report["unresolved_negative_feedback"]] == [negative_id]
    assert [row["id"] for row in report["search_issue_candidates"]] == [negative_id]


def test_usage_metadata_persists_jsonl_hash_and_index_format(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite"
    telemetry._record_usage(
        tmp_path / "root",
        tool="knowledge_search",
        success=True,
        index_metadata={"jsonl_sha256": "abc123", "index_format_version": 4},
        usage_db_path=db_path,
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT index_jsonl_sha256, index_format_version FROM usage_events").fetchone()
    assert row == ("abc123", 4)


def test_search_usage_persists_implicit_replay_boundary_and_settings(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite"
    event_id = telemetry._record_usage(
        tmp_path / "root",
        tool="knowledge_search",
        success=True,
        feedback_max_id=7,
        implicit_feedback_max_id=11,
        implicit_feedback_enabled=True,
        implicit_min_confirmations=3,
        implicit_max_generic_queries=4,
        usage_db_path=db_path,
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT feedback_max_id,
                   implicit_feedback_max_id,
                   implicit_feedback_enabled,
                   implicit_min_confirmations,
                   implicit_max_generic_queries
            FROM usage_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()

    assert row == (7, 11, 1, 3, 4)


def test_usage_migration_adds_nullable_implicit_replay_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                tool TEXT NOT NULL,
                success INTEGER NOT NULL,
                root TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO usage_events (ts, tool, success, root) VALUES ('before', 'knowledge_search', 1, 'legacy')"
        )

    telemetry._record_usage(
        tmp_path / "root",
        tool="knowledge_search",
        success=True,
        usage_db_path=db_path,
    )

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT implicit_feedback_max_id,
                   implicit_feedback_enabled,
                   implicit_min_confirmations,
                   implicit_max_generic_queries
            FROM usage_events
            ORDER BY id
            """
        ).fetchall()

    assert rows == [(None, None, None, None), (None, None, None, None)]


def test_implicit_feedback_migration_adds_nullable_turn_id(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE implicit_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                search_event_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                root TEXT NOT NULL,
                UNIQUE(search_event_id, artifact_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO implicit_feedback (
                ts, search_event_id, query, artifact_id, session_id, task_id, root
            ) VALUES ('before', 1, 'query', 'skill:a', 's', 't', 'legacy')
            """
        )

    telemetry._record_usage(
        tmp_path / "root",
        tool="knowledge_search",
        success=True,
        usage_db_path=db_path,
    )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT turn_id FROM implicit_feedback WHERE id=1").fetchone() == (
            None,
        )


def test_general_telemetry_lock_budget_is_one_second_and_usage_remains_fail_open(tmp_path: Path) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    _event(root, db_path)
    lock = sqlite3.connect(db_path, timeout=0)
    lock.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        assert telemetry._record_usage(root, tool="knowledge_search", success=True, usage_db_path=db_path) is None
        elapsed = time.monotonic() - started
    finally:
        lock.rollback()
        lock.close()
    assert 0.8 <= elapsed < 1.8


def test_feedback_lock_budget_is_strict_and_does_not_write_a_second_usage_event(tmp_path: Path) -> None:
    root = tmp_path / "root"
    db_path = tmp_path / "usage.sqlite"
    _event(root, db_path)
    lock = sqlite3.connect(db_path, timeout=0)
    lock.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises(telemetry.FeedbackDatabaseLockedError, match="temporarily locked"):
            _feedback(root, db_path, rating="other")
        elapsed = time.monotonic() - started
    finally:
        lock.rollback()
        lock.close()
    assert 0.8 <= elapsed < 1.8
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM usage_events WHERE tool='knowledge_feedback'").fetchone() == (0,)


def test_usage_report_separates_current_native_search_quality_from_operations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    usage_db = tmp_path / "usage.sqlite"

    current_ids = [
        telemetry._record_usage(
            root,
            tool="knowledge_search",
            success=True,
            query="healthy query",
            result_count=2,
            latency_ms=10,
            usage_db_path=usage_db,
        ),
        telemetry._record_usage(
            root,
            tool="knowledge_search",
            success=True,
            query="missing query",
            result_count=0,
            latency_ms=20,
            usage_db_path=usage_db,
        ),
        telemetry._record_usage(
            root,
            tool="knowledge_search",
            success=False,
            query="broken query",
            error="search failed",
            latency_ms=30,
            usage_db_path=usage_db,
        ),
    ]
    doctor_id = telemetry._record_usage(
        root,
        tool="cli_doctor",
        client="cli",
        success=True,
        latency_ms=1000,
        usage_db_path=usage_db,
    )
    old_search_id = telemetry._record_usage(
        root,
        tool="knowledge_search",
        success=False,
        query="old failure",
        error="historical failure",
        latency_ms=500,
        usage_db_path=usage_db,
    )
    current_other_id = telemetry._record_usage(
        root,
        tool="knowledge_get",
        success=True,
        latency_ms=5,
        usage_db_path=usage_db,
    )
    assert all(
        event_id is not None
        for event_id in [*current_ids, doctor_id, old_search_id, current_other_id]
    )

    with sqlite3.connect(usage_db) as connection:
        connection.execute(
            "UPDATE usage_events SET plugin_version = '0.1.0' WHERE id = ?",
            (old_search_id,),
        )
        connection.commit()

    report = telemetry._usage_report(root, days=30, limit=10, usage_db_path=usage_db)

    quality = report["current_native_search_quality"]
    assert quality["cohort"] == "current_live_native_search"
    assert quality["plugin_version"] == __version__
    assert quality["count"] == 3
    assert quality["successes"] == 2
    assert quality["errors"] == 1
    assert quality["zero_results"] == 1
    assert quality["route_changes"] == 0
    assert quality["avg_latency_ms"] == 20.0
    assert {row["query"] for row in quality["top_queries"]} == {
        "healthy query",
        "missing query",
        "broken query",
    }
    assert [row["query"] for row in quality["zero_result_queries"]] == [
        "missing query"
    ]
    assert quality["errors_by_message"][0]["error"] == "search failed"

    cohorts = {row["cohort"]: row for row in report["event_cohorts"]}
    assert cohorts["current_native_search"]["count"] == 3
    assert cohorts["cli_doctor_maintenance"]["count"] == 1
    assert cohorts["current_other_native"]["count"] == 1
    assert cohorts["historical_native_search"]["count"] == 1


def test_usage_report_implicit_consumed_rank_lower_bound_uses_current_baselines_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    other_root = tmp_path / "other-root"
    usage_db = tmp_path / "usage.sqlite"

    def search_event(
        event_root: Path,
        query: str,
        baseline: list[str],
        *,
        final: list[str] | None = None,
    ) -> int:
        final_ids = baseline if final is None else final
        event_id = telemetry._record_usage(
            event_root,
            tool="knowledge_search",
            success=True,
            query=query,
            result_count=len(final_ids),
            top_ids=final_ids,
            baseline_top_ids=baseline,
            implicit_feedback_enabled=True,
            context={
                "session_id": "session",
                "task_id": "task",
                "turn_id": "turn",
            },
            usage_db_path=usage_db,
        )
        assert event_id is not None
        return event_id

    def consume(event_root: Path, event_id: int, query: str, artifact_id: str) -> None:
        assert telemetry.record_implicit_feedback(
            event_root,
            search_event_id=event_id,
            query=query,
            artifact_id=artifact_id,
            session_id="session",
            task_id="task",
            turn_id="turn",
            usage_db_path=usage_db,
        )

    first = search_event(root, "orchid runbook", ["a", "b", "c", "d"])
    for artifact_id in ("a", "c", "d"):
        consume(root, first, "orchid runbook", artifact_id)
    second = search_event(root, "orchid docs", ["x", "y"])
    consume(root, second, "orchid docs", "y")
    missing_baseline = search_event(root, "orchid memory", [], final=["missing"])
    consume(root, missing_baseline, "orchid memory", "missing")
    malformed_baseline = search_event(root, "orchid malformed", ["malformed"])
    consume(root, malformed_baseline, "orchid malformed", "malformed")
    failed = search_event(root, "failed query", ["failed"])
    consume(root, failed, "failed query", "failed")

    historical = search_event(root, "historical query", ["historical"])
    consume(root, historical, "historical query", "historical")
    probe = search_event(root, "demo", ["probe"])
    consume(root, probe, "demo", "probe")
    cross_root = search_event(other_root, "other root query", ["other"])
    consume(other_root, cross_root, "other root query", "other")
    with sqlite3.connect(usage_db) as connection:
        connection.execute(
            "UPDATE usage_events SET plugin_version = '0.1.0' WHERE id = ?",
            (historical,),
        )
        connection.execute(
            "UPDATE usage_events SET baseline_top_ids_json = ? WHERE id = ?",
            ('{"unexpected": "malformed"}', malformed_baseline),
        )
        connection.execute(
            "UPDATE usage_events SET success = 0 WHERE id = ?",
            (failed,),
        )
        connection.commit()

    report = telemetry._usage_report(root, days=30, limit=10, usage_db_path=usage_db)
    diagnostic = report["current_native_search_quality"][
        "implicit_consumed_rank_lower_bound"
    ]

    assert diagnostic == {
        "consumed_artifact_count": 4,
        "consumed_search_count": 2,
        "ranked_consumption_count": 4,
        "unranked_consumption_count": 0,
        "consumed_at_rank_1": 1,
        "consumed_in_top_3": 3,
        "consumed_outside_top_3": 1,
        "searches_with_consumed_rank_1": 1,
        "searches_with_consumed_top_3": 2,
        "median_consumed_rank": 2.5,
        "rank_distribution": [
            {"rank": 1, "count": 1},
            {"rank": 2, "count": 1},
            {"rank": 3, "count": 1},
            {"rank": 4, "count": 1},
        ],
    }


@pytest.mark.parametrize(
    "malformation",
    [
        "orphaned_event",
        "query_mismatch",
        "session_mismatch",
        "task_mismatch",
        "empty_turn",
        "turn_mismatch",
        "root_mismatch",
        "disabled_event",
        "implicit_before_search",
        "consumption_too_late",
        "search_before_window",
        "future_consumption",
        "artifact_absent_from_baseline",
        "artifact_absent_from_final",
        "malformed_baseline",
        "malformed_final",
    ],
)
def test_usage_report_consumed_rank_excludes_malformed_implicit_linkage(
    tmp_path: Path,
    malformation: str,
) -> None:
    root = tmp_path / "root"
    usage_db = tmp_path / "usage.sqlite"
    event_id = telemetry._record_usage(
        root,
        tool="knowledge_search",
        success=True,
        query="current linked query",
        result_count=2,
        top_ids=["first", "consumed"],
        baseline_top_ids=["first", "consumed"],
        implicit_feedback_enabled=True,
        context={
            "session_id": "session",
            "task_id": "task",
            "turn_id": "turn",
        },
        usage_db_path=usage_db,
    )
    assert event_id is not None
    assert telemetry.record_implicit_feedback(
        root,
        search_event_id=event_id,
        query="current linked query",
        artifact_id="consumed",
        session_id="session",
        task_id="task",
        turn_id="turn",
        usage_db_path=usage_db,
    )

    with sqlite3.connect(usage_db) as connection:
        if malformation == "orphaned_event":
            connection.execute(
                "UPDATE implicit_feedback SET search_event_id = ?",
                (event_id + 1000,),
            )
        elif malformation == "query_mismatch":
            connection.execute("UPDATE implicit_feedback SET query = 'different query'")
        elif malformation == "session_mismatch":
            connection.execute("UPDATE implicit_feedback SET session_id = 'different-session'")
        elif malformation == "task_mismatch":
            connection.execute("UPDATE implicit_feedback SET task_id = 'different-task'")
        elif malformation == "empty_turn":
            connection.execute("UPDATE implicit_feedback SET turn_id = ''")
        elif malformation == "turn_mismatch":
            connection.execute("UPDATE usage_events SET turn_id = 'different-turn' WHERE id = ?", (event_id,))
        elif malformation == "root_mismatch":
            connection.execute("UPDATE usage_events SET root = 'different-root' WHERE id = ?", (event_id,))
        elif malformation == "disabled_event":
            connection.execute(
                "UPDATE usage_events SET implicit_feedback_enabled = 0 WHERE id = ?",
                (event_id,),
            )
        elif malformation == "implicit_before_search":
            connection.execute(
                """
                UPDATE implicit_feedback
                SET ts = (SELECT datetime(ts, '-1 second') FROM usage_events WHERE id = ?)
                """,
                (event_id,),
            )
        elif malformation == "consumption_too_late":
            connection.execute(
                "UPDATE usage_events SET ts = datetime('now', '-40 minutes') WHERE id = ?",
                (event_id,),
            )
            connection.execute(
                "UPDATE implicit_feedback SET ts = datetime('now', '-5 minutes')"
            )
        elif malformation == "search_before_window":
            connection.execute(
                """
                UPDATE usage_events
                SET ts = datetime('now', '-30 days', '-5 minutes')
                WHERE id = ?
                """,
                (event_id,),
            )
            connection.execute(
                """
                UPDATE implicit_feedback
                SET ts = datetime('now', '-30 days', '+5 minutes')
                """
            )
        elif malformation == "future_consumption":
            connection.execute(
                "UPDATE implicit_feedback SET ts = datetime('now', '+1 day')"
            )
        elif malformation == "artifact_absent_from_baseline":
            connection.execute(
                "UPDATE implicit_feedback SET artifact_id = 'not-returned'"
            )
        elif malformation == "artifact_absent_from_final":
            connection.execute(
                "UPDATE usage_events SET top_ids_json = '[\"first\"]' WHERE id = ?",
                (event_id,),
            )
        elif malformation == "malformed_baseline":
            connection.execute(
                "UPDATE usage_events SET baseline_top_ids_json = '{\"not\": \"a-list\"}' WHERE id = ?",
                (event_id,),
            )
        elif malformation == "malformed_final":
            connection.execute(
                "UPDATE usage_events SET top_ids_json = '[\"consumed\", 7]' WHERE id = ?",
                (event_id,),
            )
        else:
            raise AssertionError(f"unhandled malformation: {malformation}")

    report = telemetry._usage_report(root, days=30, limit=10, usage_db_path=usage_db)

    assert report["current_native_search_quality"][
        "implicit_consumed_rank_lower_bound"
    ] == telemetry._empty_implicit_consumed_rank_lower_bound()


@pytest.mark.parametrize("existing_empty_file", [False, True])
def test_usage_report_empty_or_missing_db_has_zero_consumed_rank_shape(
    tmp_path: Path,
    existing_empty_file: bool,
) -> None:
    usage_db = tmp_path / "usage.sqlite"
    if existing_empty_file:
        usage_db.touch()
    report = telemetry._usage_report(
        tmp_path / "root",
        days=30,
        limit=10,
        usage_db_path=usage_db,
    )

    assert report["current_native_search_quality"][
        "implicit_consumed_rank_lower_bound"
    ] == telemetry._empty_implicit_consumed_rank_lower_bound()


def test_current_search_route_details_exclude_probes_and_historical_versions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    usage_db = tmp_path / "usage.sqlite"

    telemetry._record_usage(
        root,
        tool="knowledge_search",
        success=True,
        query="current verification",
        result_count=1,
        route_feedback_id=7,
        route_artifact_id="skill:current",
        route_outcome="verification_failed",
        usage_db_path=usage_db,
    )
    telemetry._record_usage(
        root,
        tool="knowledge_search",
        success=True,
        query="current promotion",
        result_count=1,
        route_outcome="promoted_existing",
        usage_db_path=usage_db,
    )
    telemetry._record_usage(
        root,
        tool="knowledge_search",
        success=True,
        query="demo",
        result_count=1,
        route_outcome="verification_failed",
        usage_db_path=usage_db,
    )
    historical_id = telemetry._record_usage(
        root,
        tool="knowledge_search",
        success=True,
        query="historical promotion",
        result_count=1,
        route_outcome="promoted_retry",
        usage_db_path=usage_db,
    )
    resolved_zero_id = telemetry._record_usage(
        root,
        tool="knowledge_search",
        success=True,
        query="resolved zero",
        result_count=0,
        usage_db_path=usage_db,
    )
    telemetry._record_usage(
        root,
        tool="knowledge_search",
        success=True,
        query="resolved zero",
        result_count=1,
        usage_db_path=usage_db,
    )
    telemetry._record_usage(
        root,
        tool="knowledge_search",
        success=True,
        query="active zero",
        result_count=0,
        usage_db_path=usage_db,
    )
    for index in range(12):
        telemetry._record_usage(
            root,
            tool="knowledge_search",
            success=False,
            query=f"search failure {index}",
            error=f"search error {index}",
            usage_db_path=usage_db,
        )
    telemetry._record_usage(
        root,
        tool="knowledge_get",
        success=False,
        error="artifact lookup failed",
        usage_db_path=usage_db,
    )
    historical_error_id = telemetry._record_usage(
        root,
        tool="knowledge_neighbors",
        success=False,
        error="historical neighbor failure",
        usage_db_path=usage_db,
    )
    assert historical_id is not None
    assert historical_error_id is not None
    assert resolved_zero_id is not None
    with sqlite3.connect(usage_db) as connection:
        connection.execute(
            "UPDATE usage_events SET plugin_version = '0.0.0' WHERE id IN (?, ?)",
            (historical_id, historical_error_id),
        )
        connection.execute(
            "UPDATE usage_events SET ts = datetime('now', '-1 minute') WHERE id = ?",
            (resolved_zero_id,),
        )

    report = telemetry._usage_report(root, days=30, limit=10, usage_db_path=usage_db)
    current = report["current_native_search_quality"]

    assert [row["route_outcome"] for row in current["route_outcomes"]] == [
        "promoted_existing",
        "verification_failed",
    ]
    assert [row["count"] for row in current["route_outcomes"]] == [1, 1]
    assert len(current["route_verification_failures"]) == 1
    assert current["route_verification_failures"][0]["query"] == "current verification"
    assert [row["query"] for row in current["active_zero_result_queries"]] == ["active zero"]
    assert len(current["errors_by_message"]) == 10
    assert len(report["current_non_search_native_errors"]) == 1
    assert {
        key: value
        for key, value in report["current_non_search_native_errors"][0].items()
        if key != "last_seen"
    } == {
        "tool": "knowledge_get",
        "error": "artifact lookup failed",
        "count": 1,
    }
    assert report["current_non_search_native_errors"][0]["last_seen"]
    assert {row["route_outcome"] for row in report["route_outcomes"]} == {
        "promoted_existing",
        "promoted_retry",
        "verification_failed",
    }
