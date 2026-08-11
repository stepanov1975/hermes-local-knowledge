from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_local_knowledge.config import (
    Config,
    ImplicitFeedbackSettings,
    IndexSettings,
)
from hermes_local_knowledge.implicit import on_post_tool_call
from hermes_local_knowledge.telemetry import (
    IMPLICIT_FEEDBACK_ORIGIN,
    _usage_connect,
    record_implicit_feedback,
)


def _seed_search(
    usage_db_path: Path,
    root: Path,
    *,
    session_id: str,
    query: str,
    top_ids: list[str],
) -> int:
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
                json.dumps(top_ids, ensure_ascii=False),
                str(root),
            ),
        )
        connection.commit()
        lastrowid = cursor.lastrowid
        assert lastrowid is not None
        return int(lastrowid)
    finally:
        connection.close()


def _feedback_rows(usage_db_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(usage_db_path) as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT rating, origin FROM feedback ORDER BY id"
            )
        ]


def _enabled_config(
    tmp_path: Path,
) -> tuple[Config, Path, Path]:
    root = tmp_path / "root"
    state = tmp_path / "state"
    usage_db_path = state / "usage.sqlite"
    cfg = Config(
        source_root=root,
        hermes_home=tmp_path / "home",
        state_dir=state,
        index_settings=IndexSettings(),
        implicit_feedback=ImplicitFeedbackSettings(enabled=True),
    )
    return cfg, root, usage_db_path


def test_record_implicit_feedback_writes_a_verified_row(tmp_path: Path) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    search_id = _seed_search(
        usage_db_path,
        root,
        session_id="sess-1",
        query="docker image version report",
        top_ids=["runbook:target", "runbook:other"],
    )

    feedback_id = record_implicit_feedback(
        root,
        search_event_id=search_id,
        query="docker image version report",
        artifact_id="runbook:target",
        context={"session_id": "sess-1"},
        usage_db_path=usage_db_path,
    )

    assert feedback_id is not None
    with sqlite3.connect(usage_db_path) as connection:
        row = connection.execute(
            "SELECT rating, origin, linkage_status, artifact_id, query, session_id "
            "FROM feedback WHERE id = ?",
            (feedback_id,),
        ).fetchone()
    assert tuple(row) == (
        "useful",
        IMPLICIT_FEEDBACK_ORIGIN,
        "verified_event",
        "runbook:target",
        "docker image version report",
        "sess-1",
    )


def test_record_implicit_feedback_fails_open_when_artifact_is_absent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    search_id = _seed_search(
        usage_db_path,
        root,
        session_id="sess-1",
        query="docker image version report",
        top_ids=["runbook:other"],
    )

    feedback_id = record_implicit_feedback(
        root,
        search_event_id=search_id,
        query="docker image version report",
        artifact_id="runbook:target",
        context={},
        usage_db_path=usage_db_path,
    )

    assert feedback_id is None


def test_record_implicit_feedback_fails_open_on_query_mismatch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    usage_db_path = tmp_path / "state" / "usage.sqlite"
    search_id = _seed_search(
        usage_db_path,
        root,
        session_id="sess-1",
        query="docker image version report",
        top_ids=["runbook:target"],
    )

    feedback_id = record_implicit_feedback(
        root,
        search_event_id=search_id,
        query="a different query",
        artifact_id="runbook:target",
        context={},
        usage_db_path=usage_db_path,
    )

    assert feedback_id is None


def test_hook_records_implicit_feedback_for_consumed_search_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, root, usage_db_path = _enabled_config(tmp_path)
    _seed_search(
        usage_db_path,
        root,
        session_id="sess-1",
        query="docker image version report",
        top_ids=["runbook:target"],
    )
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: cfg)

    on_post_tool_call(
        tool_name="knowledge_get",
        args={"artifact_id": "runbook:target"},
        status="ok",
        session_id="sess-1",
        task_id="task-1",
        tool_call_id="call-1",
    )

    assert _feedback_rows(usage_db_path) == [("useful", IMPLICIT_FEEDBACK_ORIGIN)]


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(
            {"tool_name": "knowledge_search", "args": {"query": "anything"}},
            id="not-a-get",
        ),
        pytest.param(
            {"tool_name": "knowledge_get", "args": {"artifact_id": ""}},
            id="missing-artifact-id",
        ),
        pytest.param(
            {"tool_name": "knowledge_get", "args": {"artifact_id": "runbook:target"}, "status": "error"},
            id="failed-call",
        ),
        pytest.param(
            {"tool_name": "knowledge_get", "args": {"artifact_id": "runbook:target"}, "status": "ok"},
            id="missing-session-id",
        ),
        pytest.param(
            {"tool_name": "knowledge_get", "args": "not-a-dict", "status": "ok", "session_id": "sess-1"},
            id="malformed-args",
        ),
    ],
)
def test_hook_records_nothing_without_a_consumable_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
) -> None:
    cfg, root, usage_db_path = _enabled_config(tmp_path)
    _seed_search(
        usage_db_path,
        root,
        session_id="sess-1",
        query="docker image version report",
        top_ids=["runbook:target"],
    )
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: cfg)

    on_post_tool_call(**kwargs)

    assert _feedback_rows(usage_db_path) == []


def test_hook_records_nothing_when_artifact_not_in_search_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, root, usage_db_path = _enabled_config(tmp_path)
    _seed_search(
        usage_db_path,
        root,
        session_id="sess-1",
        query="docker image version report",
        top_ids=["runbook:other"],
    )
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: cfg)

    on_post_tool_call(
        tool_name="knowledge_get",
        args={"artifact_id": "runbook:target"},
        status="ok",
        session_id="sess-1",
    )

    assert _feedback_rows(usage_db_path) == []


def test_hook_records_nothing_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, root, usage_db_path = _enabled_config(tmp_path)
    cfg = Config(
        source_root=root,
        hermes_home=tmp_path / "home",
        state_dir=cfg.state_dir,
        index_settings=IndexSettings(),
        implicit_feedback=ImplicitFeedbackSettings(enabled=False),
    )
    _seed_search(
        usage_db_path,
        root,
        session_id="sess-1",
        query="docker image version report",
        top_ids=["runbook:target"],
    )
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: cfg)

    on_post_tool_call(
        tool_name="knowledge_get",
        args={"artifact_id": "runbook:target"},
        status="ok",
        session_id="sess-1",
    )

    assert _feedback_rows(usage_db_path) == []


def test_hook_never_raises_on_missing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config(
        source_root=tmp_path / "root",
        hermes_home=tmp_path / "home",
        state_dir=tmp_path / "state",
        index_settings=IndexSettings(),
        implicit_feedback=ImplicitFeedbackSettings(enabled=True),
    )
    monkeypatch.setattr("hermes_local_knowledge.implicit.resolve_config", lambda: cfg)

    on_post_tool_call(
        tool_name="knowledge_get",
        args={"artifact_id": "runbook:target"},
        status="ok",
        session_id="sess-1",
    )
