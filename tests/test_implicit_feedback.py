from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_local_knowledge.config import Config, ImplicitFeedbackSettings, IndexSettings
from hermes_local_knowledge.evaluation import load_positive_feedback_labels
from hermes_local_knowledge.implicit import on_post_tool_call, on_pre_llm_call, on_session_end
from hermes_local_knowledge import plugin
from hermes_local_knowledge.routing import decide_feedback_route
from hermes_local_knowledge.telemetry import _record_feedback, _record_usage, _usage_report


def _config(tmp_path: Path, *, enabled: bool = True) -> Config:
    return Config(
        source_root=tmp_path / "root",
        hermes_home=tmp_path / "home",
        state_dir=tmp_path / "state",
        index_settings=IndexSettings(),
        implicit_feedback=ImplicitFeedbackSettings(enabled=enabled, min_confirmations=2),
    )


def _search(
    config: Config,
    *,
    session: str,
    task: str,
    turn: str = "turn-1",
    query: str = "docker update progress",
    top_ids: list[str] | None = None,
    baseline_top_ids: list[str] | None = None,
) -> int:
    top_ids = top_ids or ["runbook:target", "runbook:other"]
    event_id = _record_usage(
        config.source_root,
        tool="knowledge_search",
        success=True,
        query=query,
        top_ids=top_ids,
        baseline_top_ids=top_ids if baseline_top_ids is None else baseline_top_ids,
        context={"session_id": session, "task_id": task, "turn_id": turn},
        usage_db_path=config.state_dir / "usage.sqlite",
    )
    assert event_id is not None
    return event_id


def _consume(
    monkeypatch,
    config: Config,
    *,
    session: str,
    task: str,
    turn: str = "turn-1",
    status: str | None = None,
) -> None:
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)
    on_post_tool_call(
        tool_name="knowledge_get",
        args={"artifact_id": "runbook:target"},
        result=json.dumps({"success": True}),
        status=status,
        session_id=session,
        task_id=task,
        turn_id=turn,
    )


def _decision(config: Config, *, allow_implicit: bool = True):
    return decide_feedback_route(
        [{"id": "runbook:other"}, {"id": "runbook:target"}],
        usage_db_path=config.state_dir / "usage.sqlite",
        root=config.source_root,
        query="docker update progress",
        artifact_type=None,
        db_path=tmp_path_placeholder(),
        limit=2,
        search_index_fn=lambda *args, **kwargs: [],
        allow_implicit=allow_implicit,
        implicit_min_confirmations=2,
    )


def tmp_path_placeholder() -> Path:
    return Path("/unused/index.sqlite")


def test_same_search_consumption_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _search(config, session="s1", task="t1")

    _consume(monkeypatch, config, session="s1", task="t1")
    _consume(monkeypatch, config, session="s1", task="t1")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 1


@pytest.mark.parametrize(
    "baseline_json",
    [
        "not-json",
        json.dumps({"artifact": "runbook:target"}),
        json.dumps(["runbook:target", 7]),
        json.dumps([None, "runbook:target"]),
    ],
)
def test_malformed_baseline_cannot_create_implicit_feedback(
    tmp_path: Path,
    monkeypatch,
    baseline_json: str,
) -> None:
    config = _config(tmp_path)
    event_id = _search(config, session="s1", task="t1")
    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        connection.execute(
            "UPDATE usage_events SET baseline_top_ids_json = ? WHERE id = ?",
            (baseline_json, event_id),
        )

    _consume(monkeypatch, config, session="s1", task="t1")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_lifecycle_turn_context_supports_bridge_hooks_missing_turn_id(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    event_id = _record_usage(
        config.source_root,
        tool="knowledge_search",
        success=True,
        query="docker update progress",
        top_ids=["runbook:target"],
        baseline_top_ids=["runbook:target"],
        context={"session_id": "s1", "task_id": "t1"},
        usage_db_path=config.state_dir / "usage.sqlite",
    )
    assert event_id is not None
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)
    on_pre_llm_call(session_id="s1", task_id="t1", turn_id="turn-1")
    try:
        on_post_tool_call(
            tool_name="knowledge_search",
            args={"query": "docker update progress"},
            result=json.dumps({"success": True, "usage_event_id": event_id}),
            session_id="s1",
            task_id="t1",
        )
        on_post_tool_call(
            tool_name="knowledge_get",
            args={"artifact_id": "runbook:target"},
            result=json.dumps({"success": True}),
            session_id="s1",
            task_id="t1",
        )
    finally:
        on_session_end()

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute(
            "SELECT turn_id FROM usage_events WHERE id = ?", (event_id,)
        ).fetchone() == ("turn-1",)
        assert connection.execute(
            "SELECT search_event_id, turn_id FROM implicit_feedback"
        ).fetchone() == (event_id, "turn-1")


def test_lifecycle_turn_context_requires_matching_session_and_task(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    event_id = _record_usage(
        config.source_root,
        tool="knowledge_search",
        success=True,
        query="docker update progress",
        top_ids=["runbook:target"],
        baseline_top_ids=["runbook:target"],
        context={"session_id": "s1", "task_id": "other-task"},
        usage_db_path=config.state_dir / "usage.sqlite",
    )
    assert event_id is not None
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)
    on_pre_llm_call(session_id="s1", task_id="t1", turn_id="turn-1")
    try:
        on_post_tool_call(
            tool_name="knowledge_search",
            result=json.dumps({"success": True, "usage_event_id": event_id}),
            session_id="s1",
            task_id="other-task",
        )
    finally:
        on_session_end()

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute(
            "SELECT turn_id FROM usage_events WHERE id = ?", (event_id,)
        ).fetchone() == (None,)


def test_session_end_clears_lifecycle_turn_context(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _search(config, session="s1", task="t1", turn="turn-1")
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)
    on_pre_llm_call(session_id="s1", task_id="t1", turn_id="turn-1")
    on_session_end()

    on_post_tool_call(
        tool_name="knowledge_get",
        args={"artifact_id": "runbook:target"},
        result=json.dumps({"success": True}),
        session_id="s1",
        task_id="t1",
    )

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_different_turn_does_not_credit_recent_search(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _search(config, session="s1", task="t1", turn="search-turn")

    _consume(monkeypatch, config, session="s1", task="t1", turn="later-turn")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_route_only_result_does_not_create_implicit_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    _search(
        config,
        session="s1",
        task="t1",
        top_ids=["runbook:target", "runbook:other"],
        baseline_top_ids=["runbook:other"],
    )

    _consume(monkeypatch, config, session="s1", task="t1")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_truncated_baseline_result_does_not_create_implicit_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    _search(
        config,
        session="s1",
        task="t1",
        top_ids=["runbook:other"],
        baseline_top_ids=["runbook:target"],
    )

    _consume(monkeypatch, config, session="s1", task="t1")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_search_from_another_root_does_not_create_implicit_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    event_id = _record_usage(
        tmp_path / "old-root",
        tool="knowledge_search",
        success=True,
        query="docker update progress",
        top_ids=["runbook:target"],
        baseline_top_ids=["runbook:target"],
        context={"session_id": "s1", "task_id": "t1", "turn_id": "turn-1"},
        usage_db_path=config.state_dir / "usage.sqlite",
    )
    assert event_id is not None

    _consume(monkeypatch, config, session="s1", task="t1")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_semantically_failed_get_is_not_positive_evidence(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _search(config, session="s1", task="t1")
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)

    on_post_tool_call(
        tool_name="knowledge_get",
        args={"artifact_id": "runbook:target"},
        status="ok",
        result=json.dumps(
            {
                "success": False,
                "error": "Artifact not found: runbook:target",
                "artifact_id": "runbook:target",
                "usage_event_id": 2,
            }
        ),
        session_id="s1",
        task_id="t1",
        turn_id="turn-1",
    )

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_failed_status_overrides_success_payload(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _search(config, session="s1", task="t1")
    _consume(monkeypatch, config, session="s1", task="t1", status="error")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_requires_two_distinct_searches_before_routing(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _search(config, session="s1", task="t1")
    _consume(monkeypatch, config, session="s1", task="t1")
    assert _decision(config).rows[0]["id"] == "runbook:other"

    _search(config, session="s1", task="t1")
    _consume(monkeypatch, config, session="s1", task="t1")
    decision = _decision(config)
    assert decision.rows[0]["id"] == "runbook:target"
    assert decision.feedback_id is None
    assert decision.feedback_max_id == 0
    assert decision.implicit_feedback_max_id == 2
    assert load_positive_feedback_labels(config.state_dir / "usage.sqlite") == {}
    report = _usage_report(
        config.source_root,
        days=1,
        limit=5,
        usage_db_path=config.state_dir / "usage.sqlite",
    )
    assert report["feedback_count"] == 0
    assert report["implicit_feedback_count"] == 2


@pytest.mark.parametrize(
    ("corruption_sql", "params"),
    [
        ("UPDATE implicit_feedback SET search_event_id=999 WHERE id=2", ()),
        ("UPDATE implicit_feedback SET query='wrong query' WHERE id=2", ()),
        ("UPDATE implicit_feedback SET session_id='wrong-session' WHERE id=2", ()),
        ("UPDATE implicit_feedback SET task_id='wrong-task' WHERE id=2", ()),
        ("UPDATE implicit_feedback SET turn_id='wrong-turn' WHERE id=2", ()),
        ("UPDATE implicit_feedback SET turn_id='' WHERE id=2", ()),
        ("UPDATE usage_events SET tool='knowledge_get' WHERE id=2", ()),
        ("UPDATE usage_events SET success='invalid' WHERE id=2", ()),
        ("UPDATE usage_events SET root=? WHERE id=2", ("__OTHER_ROOT__",)),
        ("UPDATE usage_events SET query='wrong query' WHERE id=2", ()),
        ("UPDATE usage_events SET session_id='wrong-session' WHERE id=2", ()),
        ("UPDATE usage_events SET task_id='wrong-task' WHERE id=2", ()),
        ("UPDATE usage_events SET turn_id='wrong-turn' WHERE id=2", ()),
        ("UPDATE usage_events SET baseline_top_ids_json='not-json' WHERE id=2", ()),
        (
            "UPDATE usage_events SET baseline_top_ids_json='[\"runbook:target\", 7]' WHERE id=2",
            (),
        ),
        (
            "UPDATE usage_events SET baseline_top_ids_json='[\"runbook:other\"]' WHERE id=2",
            (),
        ),
        ("UPDATE usage_events SET top_ids_json='[\"runbook:other\"]' WHERE id=2", ()),
    ],
)
def test_live_implicit_routing_rejects_malformed_linked_provenance(
    tmp_path: Path,
    monkeypatch,
    corruption_sql: str,
    params: tuple[str, ...],
) -> None:
    config = _config(tmp_path)
    for session, task in (("s1", "t1"), ("s2", "t2")):
        _search(config, session=session, task=task)
        _consume(monkeypatch, config, session=session, task=task)
    usage_db = config.state_dir / "usage.sqlite"
    resolved_params = tuple(
        str(tmp_path / "other-root") if value == "__OTHER_ROOT__" else value for value in params
    )
    with sqlite3.connect(usage_db) as connection:
        connection.execute(corruption_sql, resolved_params)

    decision = _decision(config)

    assert decision.rows[0]["id"] == "runbook:other"
    assert decision.feedback_max_id == 0
    assert decision.implicit_feedback_max_id == 2


def test_exact_implicit_route_tie_is_stable_across_hash_seeds(tmp_path: Path) -> None:
    config = _config(tmp_path)
    usage_db = config.state_dir / "usage.sqlite"
    implicit_rows = []
    for artifact_id, route_query in (
        ("runbook:a", "docker update progress"),
        ("runbook:b", "docker update progress"),
        ("runbook:b", "docker release progress"),
    ):
        for occurrence in range(2):
            session = f"{artifact_id}-{occurrence}"
            task = f"task-{occurrence}"
            turn = f"turn-{occurrence}"
            search_event_id = _search(
                config,
                session=session,
                task=task,
                turn=turn,
                query=route_query,
                top_ids=[artifact_id],
                baseline_top_ids=[artifact_id],
            )
            implicit_rows.append(
                (
                    "2026-08-12T00:00:00+00:00",
                    search_event_id,
                    route_query,
                    artifact_id,
                    session,
                    task,
                    turn,
                    str(config.source_root),
                )
            )
    with sqlite3.connect(usage_db) as connection:
        connection.executemany(
            """
            INSERT INTO implicit_feedback (
                ts, search_event_id, query, artifact_id, session_id, task_id, turn_id, root
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            implicit_rows,
        )

    script = """
import sys
from pathlib import Path
from hermes_local_knowledge.routing import _implicit_feedback_route

route = _implicit_feedback_route(
    Path(sys.argv[1]),
    root=Path(sys.argv[2]),
    query="docker update release progress",
    artifact_type=None,
    min_confirmations=2,
    max_generic_queries=5,
)
print(f"{route.artifact_id}|{route.query}" if route else "")
"""
    winners = set()
    for seed in ("1", "2", "3", "4", "random"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(usage_db), str(config.source_root)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        winners.add(result.stdout.strip())

    assert winners == {"runbook:b|docker update progress"}


def test_overly_generic_artifact_is_not_promoted(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    queries = (
        "docker update progress",
        "docker release status",
        "docker deployment tracking",
        "docker upgrade report",
        "docker container changes",
        "docker stack history",
    )
    for query in queries:
        for occurrence in range(2):
            _search(config, session="s1", task="t1", query=query)
            _consume(monkeypatch, config, session="s1", task="t1")

    decision = decide_feedback_route(
        [{"id": "runbook:other"}, {"id": "runbook:target"}],
        usage_db_path=config.state_dir / "usage.sqlite",
        root=config.source_root,
        query="docker update progress",
        artifact_type=None,
        db_path=tmp_path_placeholder(),
        limit=2,
        search_index_fn=lambda *args, **kwargs: [],
        allow_implicit=True,
        implicit_min_confirmations=2,
        implicit_max_generic_queries=1,
    )

    assert decision.rows[0]["id"] == "runbook:other"


def test_disabled_feature_neither_records_nor_routes(tmp_path: Path, monkeypatch) -> None:
    enabled = _config(tmp_path)
    for session, task in (("s1", "t1"), ("s2", "t2")):
        _search(enabled, session=session, task=task)
        _consume(monkeypatch, enabled, session=session, task=task)

    disabled = _config(tmp_path, enabled=False)
    _search(disabled, session="s3", task="t3")
    _consume(monkeypatch, disabled, session="s3", task="t3")

    assert _decision(disabled, allow_implicit=False).rows[0]["id"] == "runbook:other"
    with sqlite3.connect(disabled.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 2


def test_only_latest_same_task_recent_search_is_eligible(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _search(config, session="s1", task="other")
    _consume(monkeypatch, config, session="s1", task="wanted")
    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0

    event_id = _search(config, session="s1", task="wanted")
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        connection.execute("UPDATE usage_events SET ts = ? WHERE id = ?", (old, event_id))
    _consume(monkeypatch, config, session="s1", task="wanted")
    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_latest_eligible_search_can_precede_a_refinement(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    matching_event = _search(config, session="s1", task="wanted")
    _record_usage(
        config.source_root,
        tool="knowledge_search",
        success=True,
        query="docker narrower refinement",
        top_ids=["runbook:other"],
        context={"session_id": "s1", "task_id": "wanted", "turn_id": "turn-1"},
        usage_db_path=config.state_dir / "usage.sqlite",
    )

    _consume(monkeypatch, config, session="s1", task="wanted")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute(
            "SELECT search_event_id FROM implicit_feedback"
        ).fetchone() == (matching_event,)


def test_explicit_route_takes_precedence_over_implicit(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    for session, task in (("s1", "t1"), ("s2", "t2")):
        _search(config, session=session, task=task)
        _consume(monkeypatch, config, session=session, task=task)
    _record_feedback(
        config.source_root,
        rating="useful",
        event_id=None,
        query="docker update progress",
        artifact_id="runbook:other",
        note="explicit choice",
        context={},
        usage_db_path=config.state_dir / "usage.sqlite",
    )

    assert _decision(config).rows[0]["id"] == "runbook:other"


def test_partial_implicit_schema_does_not_break_explicit_routing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    usage_db = config.state_dir / "usage.sqlite"
    _record_feedback(
        config.source_root,
        rating="useful",
        event_id=None,
        query="docker update progress",
        artifact_id="runbook:target",
        note="explicit choice",
        context={},
        usage_db_path=usage_db,
    )
    with sqlite3.connect(usage_db) as connection:
        connection.execute("DROP TABLE implicit_feedback")
        connection.execute(
            "CREATE TABLE implicit_feedback "
            "(id INTEGER PRIMARY KEY, query TEXT, artifact_id TEXT)"
        )
        connection.execute(
            "INSERT INTO implicit_feedback (id, query, artifact_id) VALUES (1, ?, ?)",
            ("docker update progress", "runbook:other"),
        )

    decision = _decision(config)

    assert decision.rows[0]["id"] == "runbook:target"
    assert decision.feedback_max_id == 1
    assert decision.implicit_feedback_max_id is None


@pytest.mark.parametrize("with_explicit_route", [False, True])
def test_malformed_implicit_high_water_fails_open(
    tmp_path: Path,
    with_explicit_route: bool,
) -> None:
    config = _config(tmp_path)
    usage_db = config.state_dir / "usage.sqlite"
    _record_feedback(
        config.source_root,
        rating="useful" if with_explicit_route else "other",
        event_id=None,
        query="docker update progress",
        artifact_id="runbook:target",
        note="explicit choice" if with_explicit_route else "neutral",
        context={},
        usage_db_path=usage_db,
    )
    with sqlite3.connect(usage_db) as connection:
        connection.execute("DROP TABLE implicit_feedback")
        connection.execute("CREATE TABLE implicit_feedback (id INTEGER, root TEXT)")
        connection.execute(
            "INSERT INTO implicit_feedback (id, root) VALUES (?, ?)",
            ("bad-id", str(config.source_root)),
        )

    decision = _decision(config)

    assert decision.rows[0]["id"] == (
        "runbook:target" if with_explicit_route else "runbook:other"
    )
    assert decision.feedback_max_id == 1
    assert decision.implicit_feedback_max_id is None


def test_malformed_explicit_high_water_fails_open(tmp_path: Path) -> None:
    config = _config(tmp_path)
    usage_db = config.state_dir / "usage.sqlite"
    _record_feedback(
        config.source_root,
        rating="useful",
        event_id=None,
        query="docker update progress",
        artifact_id="runbook:target",
        note="explicit choice",
        context={},
        usage_db_path=usage_db,
    )
    with sqlite3.connect(usage_db) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ALTER TABLE feedback RENAME TO feedback_valid")
        connection.execute(
            "CREATE TABLE feedback ("
            "id INTEGER, root TEXT, rating TEXT, query TEXT, artifact_id TEXT, event_id INTEGER)"
        )
        connection.execute(
            "INSERT INTO feedback (id, root, rating, query, artifact_id, event_id) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (
                "bad-id",
                str(config.source_root),
                "useful",
                "docker update progress",
                "runbook:target",
            ),
        )

    decision = _decision(config)

    assert decision.rows[0]["id"] == "runbook:other"
    assert decision.feedback_max_id is None
    assert decision.implicit_feedback_max_id == 0


def test_explicit_rejection_vetoes_implicit_route(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    for session, task in (("s1", "t1"), ("s2", "t2")):
        _search(config, session=session, task=task)
        _consume(monkeypatch, config, session=session, task=task)
    _record_feedback(
        config.source_root,
        rating="not_useful",
        event_id=None,
        query="docker update progress",
        artifact_id="runbook:target",
        note="explicit rejection",
        context={},
        usage_db_path=config.state_dir / "usage.sqlite",
    )

    decision = decide_feedback_route(
        [{"id": "runbook:other"}, {"id": "runbook:target"}],
        usage_db_path=config.state_dir / "usage.sqlite",
        root=config.source_root,
        query="docker update progress needs attention",
        artifact_type=None,
        db_path=tmp_path_placeholder(),
        limit=2,
        search_index_fn=lambda *args, **kwargs: [],
        allow_implicit=True,
        implicit_min_confirmations=2,
    )

    assert decision.rows[0]["id"] == "runbook:other"


def test_implicit_route_retains_both_historical_replay_bounds(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    for occurrence in range(2):
        _search(config, session="s1", task="t1")
        _consume(monkeypatch, config, session="s1", task="t1")

    decision = _decision(config)

    assert decision.rows[0]["id"] == "runbook:target"
    assert decision.feedback_max_id == 0
    assert decision.implicit_feedback_max_id == 2


def test_explicit_route_still_captures_implicit_high_water(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    for session, task in (("s1", "t1"), ("s2", "t2")):
        _search(config, session=session, task=task)
        _consume(monkeypatch, config, session=session, task=task)
    _record_feedback(
        config.source_root,
        rating="useful",
        event_id=None,
        query="docker update progress",
        artifact_id="runbook:other",
        note="explicit choice",
        context={},
        usage_db_path=config.state_dir / "usage.sqlite",
    )

    decision = _decision(config)

    assert decision.rows[0]["id"] == "runbook:other"
    assert decision.feedback_max_id == 1
    assert decision.implicit_feedback_max_id == 2


def test_plugin_post_tool_hook_runs_okf_and_implicit_handlers(monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(plugin, "_on_okf_post_tool_call", lambda **_kwargs: called.append("okf"))
    monkeypatch.setattr(
        plugin,
        "_on_implicit_post_tool_call",
        lambda **_kwargs: called.append("implicit"),
    )

    plugin._on_post_tool_call(tool_name="knowledge_get")

    assert called == ["okf", "implicit"]
