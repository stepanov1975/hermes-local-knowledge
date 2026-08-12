from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_local_knowledge.config import Config, ImplicitFeedbackSettings, IndexSettings
from hermes_local_knowledge.evaluation import load_positive_feedback_labels
from hermes_local_knowledge.implicit import on_post_tool_call
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


def _search(config: Config, *, session: str, task: str, query: str = "docker update progress") -> int:
    event_id = _record_usage(
        config.source_root,
        tool="knowledge_search",
        success=True,
        query=query,
        top_ids=["runbook:target", "runbook:other"],
        context={"session_id": session, "task_id": task},
        usage_db_path=config.state_dir / "usage.sqlite",
    )
    assert event_id is not None
    return event_id


def _consume(monkeypatch, config: Config, *, session: str, task: str) -> None:
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: config)
    on_post_tool_call(
        tool_name="knowledge_get",
        args={"artifact_id": "runbook:target"},
        result=json.dumps({"success": True}),
        session_id=session,
        task_id=task,
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
    )

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
    assert load_positive_feedback_labels(config.state_dir / "usage.sqlite") == {}
    report = _usage_report(
        config.source_root,
        days=1,
        limit=5,
        usage_db_path=config.state_dir / "usage.sqlite",
    )
    assert report["feedback_count"] == 0
    assert report["implicit_feedback_count"] == 2


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
        context={"session_id": "s1", "task_id": "wanted"},
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


def test_implicit_route_marks_historical_replay_bound_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    for occurrence in range(2):
        _search(config, session="s1", task="t1")
        _consume(monkeypatch, config, session="s1", task="t1")

    assert _decision(config).feedback_max_id is None


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
