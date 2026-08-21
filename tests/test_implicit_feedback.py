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
from hermes_local_knowledge import plugin, routing
from hermes_local_knowledge.index import build_index, index_metadata
from hermes_local_knowledge.routing import decide_feedback_route
from hermes_local_knowledge.telemetry import (
    _clean_text,
    _record_feedback,
    _record_usage,
    _usage_report,
)


def _config(tmp_path: Path, *, enabled: bool = True) -> Config:
    return Config(
        source_root=tmp_path / "root",
        hermes_home=tmp_path / "home",
        state_dir=tmp_path / "state",
        index_settings=IndexSettings(),
        implicit_feedback=ImplicitFeedbackSettings(enabled=enabled, min_confirmations=2),
    )


def test_unrelated_post_tool_does_not_resolve_implicit_config(monkeypatch) -> None:
    resolutions = 0

    def unexpected_config_resolution() -> Config:
        nonlocal resolutions
        resolutions += 1
        raise AssertionError("unrelated tools must not resolve implicit-feedback config")

    monkeypatch.setattr(
        "hermes_local_knowledge.implicit.resolve_config",
        unexpected_config_resolution,
    )

    on_post_tool_call(tool_name="terminal", result=json.dumps({"success": True}))

    assert resolutions == 0


@pytest.mark.parametrize("tool_name", [[], {}], ids=["list", "mapping"])
def test_unhashable_tool_name_remains_fail_open(tool_name: object) -> None:
    on_post_tool_call(tool_name=tool_name)


def _search(
    config: Config,
    *,
    session: str,
    task: str,
    turn: str = "turn-1",
    query: str = "docker update progress",
    top_ids: list[str] | None = None,
    baseline_top_ids: list[str] | None = None,
    metadata: dict[str, object] | None = None,
    api_request_id: str = "search-request",
) -> int:
    top_ids = top_ids or ["runbook:target", "runbook:other"]
    event_id = _record_usage(
        config.source_root,
        tool="knowledge_search",
        success=True,
        query=query,
        top_ids=top_ids,
        baseline_top_ids=top_ids if baseline_top_ids is None else baseline_top_ids,
        implicit_feedback_enabled=config.implicit_feedback.enabled,
        implicit_min_confirmations=config.implicit_feedback.min_confirmations,
        implicit_max_generic_queries=config.implicit_feedback.max_generic_queries,
        index_metadata=metadata,
        context={
            "session_id": session,
            "task_id": task,
            "turn_id": turn,
            "api_request_id": api_request_id,
        },
        usage_db_path=config.state_dir / "usage.sqlite",
    )
    assert event_id is not None
    return event_id


def _consumer_index(config: Config) -> tuple[str, Path, str, Path, dict[str, object]]:
    skill_path = config.source_root / "custom_skills" / "test-router" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: test-router\ndescription: Route test operations.\n---\n\n# Test Router\n",
        encoding="utf-8",
    )
    runbook_path = config.source_root / "docs" / "test-update.md"
    runbook_path.parent.mkdir(parents=True)
    runbook_path.write_text("# Test update runbook\n", encoding="utf-8")
    artifacts_and_edges = build_index(
        config.source_root,
        config.state_dir,
        config.hermes_home,
        config.index_settings,
    )
    assert artifacts_and_edges is not None
    artifacts, _edges = artifacts_and_edges
    skill_id = next(artifact.id for artifact in artifacts if artifact.title == "test-router")
    runbook_id = next(
        artifact.id for artifact in artifacts if artifact.path == "docs/test-update.md"
    )
    metadata = index_metadata(config.state_dir / "index.sqlite")
    return skill_id, skill_path, runbook_id, runbook_path, metadata


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


@pytest.mark.parametrize("consumer", ["skill_view", "read_file"])
def test_exact_file_backed_consumer_records_implicit_feedback(
    tmp_path: Path,
    monkeypatch,
    consumer: str,
) -> None:
    config = _config(tmp_path)
    skill_id, skill_path, runbook_id, runbook_path, metadata = _consumer_index(config)
    artifact_id = skill_id if consumer == "skill_view" else runbook_id
    event_id = _search(
        config,
        session="s1",
        task="t1",
        top_ids=[artifact_id],
        metadata=metadata,
    )
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)
    if consumer == "skill_view":
        on_post_tool_call(
            tool_name="skill_view",
            args={"name": "test-router"},
            result=json.dumps({"success": True, "_source_path": str(skill_path)}),
            session_id="s1",
            task_id="t1",
            turn_id="turn-1",
            api_request_id="consumer-request",
        )
    else:
        on_post_tool_call(
            tool_name="read_file",
            args={"path": str(runbook_path)},
            result=json.dumps({"content": "# Test update runbook", "total_lines": 1}),
            session_id="s1",
            task_id="t1",
            turn_id="turn-1",
            api_request_id="consumer-request",
        )

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute(
            "SELECT search_event_id, artifact_id, consumer_tool FROM implicit_feedback"
        ).fetchall() == [(event_id, artifact_id, consumer)]


@pytest.mark.parametrize("api_request_id", ["search-request", ""])
def test_file_backed_consumer_requires_a_later_model_request(
    tmp_path: Path,
    monkeypatch,
    api_request_id: str,
) -> None:
    config = _config(tmp_path)
    skill_id, skill_path, _runbook_id, _runbook_path, metadata = _consumer_index(config)
    _search(
        config,
        session="s1",
        task="t1",
        top_ids=[skill_id],
        metadata=metadata,
    )
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)

    on_post_tool_call(
        tool_name="skill_view",
        args={"name": "test-router"},
        result=json.dumps({"success": True, "_source_path": str(skill_path)}),
        session_id="s1",
        task_id="t1",
        turn_id="turn-1",
        api_request_id=api_request_id,
    )

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone() == (0,)


@pytest.mark.parametrize("consumer", ["skill_view", "read_file"])
def test_file_backed_consumer_rejects_nonmatching_path_or_index_snapshot(
    tmp_path: Path,
    monkeypatch,
    consumer: str,
) -> None:
    config = _config(tmp_path)
    skill_id, skill_path, runbook_id, runbook_path, metadata = _consumer_index(config)
    artifact_id = skill_id if consumer == "skill_view" else runbook_id
    event_id = _search(
        config,
        session="s1",
        task="t1",
        top_ids=[artifact_id],
        metadata=metadata,
    )
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)

    if consumer == "skill_view":
        call = {
            "tool_name": "skill_view",
            "args": {"name": "test-router"},
            "result": json.dumps(
                {"success": True, "_source_path": str(skill_path.with_name("OTHER.md"))}
            ),
        }
    else:
        call = {
            "tool_name": "read_file",
            "args": {"path": str(runbook_path.with_name("other.md"))},
            "result": json.dumps({"content": "other", "total_lines": 1}),
        }
    on_post_tool_call(
        **call,
        session_id="s1",
        task_id="t1",
        turn_id="turn-1",
        api_request_id="consumer-request",
    )

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone() == (0,)
        connection.execute(
            "UPDATE usage_events SET index_jsonl_sha256 = ? WHERE id = ?",
            ("0" * 64, event_id),
        )
        connection.commit()

    if consumer == "skill_view":
        call["result"] = json.dumps({"success": True, "_source_path": str(skill_path)})
    else:
        call["args"] = {"path": str(runbook_path)}
    on_post_tool_call(
        **call,
        session_id="s1",
        task_id="t1",
        turn_id="turn-1",
        api_request_id="consumer-request",
    )
    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone() == (0,)


def test_file_backed_consumer_rejects_route_assisted_only_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    skill_id, skill_path, runbook_id, _runbook_path, metadata = _consumer_index(config)
    _search(
        config,
        session="s1",
        task="t1",
        top_ids=[skill_id],
        baseline_top_ids=[runbook_id],
        metadata=metadata,
    )
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)

    on_post_tool_call(
        tool_name="skill_view",
        args={"name": "test-router"},
        result=json.dumps({"success": True, "_source_path": str(skill_path)}),
        session_id="s1",
        task_id="t1",
        turn_id="turn-1",
        api_request_id="consumer-request",
    )

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone() == (0,)


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
    session = "  session   " + "x" * 160
    task = " task\t" + "y" * 160
    turn = " turn\n" + "z" * 160
    clean_session = _clean_text(session, limit=128)
    clean_task = _clean_text(task, limit=128)
    clean_turn = _clean_text(turn, limit=128)
    event_id = _record_usage(
        config.source_root,
        tool="knowledge_search",
        success=True,
        query="docker update progress",
        top_ids=["runbook:target"],
        baseline_top_ids=["runbook:target"],
        context={"session_id": clean_session, "task_id": clean_task},
        usage_db_path=config.state_dir / "usage.sqlite",
    )
    assert event_id is not None
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)
    on_pre_llm_call(session_id=session, task_id=task, turn_id=turn)
    try:
        on_post_tool_call(
            tool_name="knowledge_search",
            args={"query": "docker update progress"},
            result=json.dumps({"success": True, "usage_event_id": event_id}),
            session_id=session,
            task_id=task,
        )
        on_post_tool_call(
            tool_name="knowledge_get",
            args={"artifact_id": "runbook:target"},
            result=json.dumps({"success": True}),
            session_id=session,
            task_id=task,
        )
    finally:
        on_session_end()

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute(
            "SELECT turn_id FROM usage_events WHERE id = ?", (event_id,)
        ).fetchone() == (clean_turn,)
        assert connection.execute(
            "SELECT search_event_id, session_id, task_id, turn_id FROM implicit_feedback"
        ).fetchone() == (event_id, clean_session, clean_task, clean_turn)


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


def test_implicit_route_reads_the_high_water_once(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    for session, task in (("s1", "t1"), ("s2", "t2")):
        _search(config, session=session, task=task)
        _consume(monkeypatch, config, session=session, task=task)

    original = routing._implicit_feedback_high_water
    calls = 0

    def counting_high_water(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(routing, "_implicit_feedback_high_water", counting_high_water)

    assert _decision(config).rows[0]["id"] == "runbook:target"
    assert calls == 1


def test_future_persisted_confirmations_do_not_route(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    for session, task in (("s1", "t1"), ("s2", "t2")):
        _search(config, session=session, task=task)
        _consume(monkeypatch, config, session=session, task=task)
    future_search = datetime.now(timezone.utc) + timedelta(hours=1)
    future_consumption = future_search + timedelta(minutes=1)
    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        connection.execute("UPDATE usage_events SET ts = ?", (future_search.isoformat(),))
        connection.execute("UPDATE implicit_feedback SET ts = ?", (future_consumption.isoformat(),))

    assert _decision(config).rows[0]["id"] == "runbook:other"


@pytest.mark.parametrize(
    ("corruption_sql", "params"),
    [
        ("UPDATE implicit_feedback SET search_event_id=999 WHERE id=2", ()),
        ("UPDATE implicit_feedback SET query='wrong query' WHERE id=2", ()),
        ("UPDATE implicit_feedback SET session_id='wrong-session' WHERE id=2", ()),
        ("UPDATE implicit_feedback SET task_id='wrong-task' WHERE id=2", ()),
        ("UPDATE implicit_feedback SET turn_id='wrong-turn' WHERE id=2", ()),
        ("UPDATE implicit_feedback SET turn_id='' WHERE id=2", ()),
        ("UPDATE implicit_feedback SET ts=datetime(ts, '+31 minutes') WHERE id=2", ()),
        ("UPDATE usage_events SET tool='knowledge_get' WHERE id=2", ()),
        ("UPDATE usage_events SET success='invalid' WHERE id=2", ()),
        ("UPDATE usage_events SET implicit_feedback_enabled=0 WHERE id=2", ()),
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
                    datetime.now(timezone.utc).isoformat(),
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


def test_future_search_is_not_eligible(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    event_id = _search(config, session="s1", task="wanted")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        connection.execute("UPDATE usage_events SET ts = ? WHERE id = ?", (future, event_id))
    _consume(monkeypatch, config, session="s1", task="wanted")
    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 0


def test_hook_identity_normalization_matches_telemetry_storage(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    session = "  session   " + "x" * 160
    task = " task\t" + "y" * 160
    turn = " turn\n" + "z" * 160
    _search(
        config,
        session=_clean_text(session, limit=128),
        task=_clean_text(task, limit=128),
        turn=_clean_text(turn, limit=128),
        query="wanted",
    )

    _consume(monkeypatch, config, session=session, task=task, turn=turn)

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone()[0] == 1


def test_stale_newer_id_does_not_hide_recent_search(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    recent_event = _search(config, session="s1", task="wanted")
    stale_event = _search(config, session="s1", task="wanted")
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        connection.execute("UPDATE usage_events SET ts = ? WHERE id = ?", (old, stale_event))

    _consume(monkeypatch, config, session="s1", task="wanted")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute(
            "SELECT search_event_id FROM implicit_feedback"
        ).fetchone() == (recent_event,)


def test_empty_query_newer_id_does_not_hide_recent_search(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    recent_event = _search(config, session="s1", task="wanted")
    _search(config, session="s1", task="wanted", query=" ")

    _consume(monkeypatch, config, session="s1", task="wanted")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute(
            "SELECT search_event_id FROM implicit_feedback"
        ).fetchone() == (recent_event,)


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


def test_route_assisted_newer_search_blocks_older_attribution(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    _search(config, session="s1", task="wanted")
    _search(
        config,
        session="s1",
        task="wanted",
        query="docker narrower refinement",
        top_ids=["runbook:target"],
        baseline_top_ids=["runbook:other"],
    )

    _consume(monkeypatch, config, session="s1", task="wanted")

    with sqlite3.connect(config.state_dir / "usage.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone() == (0,)


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
