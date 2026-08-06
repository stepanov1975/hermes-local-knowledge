from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

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
            "baseline_top_ids_json", "route_feedback_id", "route_artifact_id", "route_outcome",
            "index_jsonl_sha256", "index_format_version", "feedback_max_id",
        } <= usage_columns
        assert {"expected_artifact_id", "resolves_feedback_id", "linkage_status"} <= feedback_columns
        assert conn.execute(
            "SELECT feedback_max_id, baseline_top_ids_json, route_outcome FROM usage_events WHERE id=1"
        ).fetchone() == (-1, "[]", "none")
        assert conn.execute("SELECT linkage_status FROM feedback WHERE id=1").fetchone() == ("legacy",)
        indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list(feedback)")}
        assert {"idx_feedback_resolution_unique", "idx_feedback_route_lookup"} <= indexes
        # Simulate a rolled-back v0.4.2 writer that knows none of the additive columns.
        conn.execute(
            "INSERT INTO usage_events (ts, tool, success, root) VALUES ('rollback', 'knowledge_search', 1, ?)",
            (str(root),),
        )
        rollback = conn.execute(
            "SELECT feedback_max_id, baseline_top_ids_json, route_outcome FROM usage_events WHERE ts='rollback'"
        ).fetchone()
    assert rollback == (-1, "[]", "none")


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
