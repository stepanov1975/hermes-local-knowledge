from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hermes_local_knowledge import index
from hermes_local_knowledge.artifacts import Artifact, Edge
from hermes_local_knowledge.config import Config, IndexSettings
from hermes_local_knowledge.service import LocalKnowledgeService


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_config(tmp_path: Path) -> Config:
    root = (tmp_path / "source").resolve()
    hermes_home = (tmp_path / "hermes-home").resolve()
    state_dir = (tmp_path / "state").resolve()
    root.mkdir()
    hermes_home.mkdir()
    write(
        root / "custom_skills" / "quartz-router" / "SKILL.md",
        """---
name: quartz-router
description: Route Quartz inventory operations.
metadata:
  hermes:
    related_skills: [quartz-helper]
---
# Quartz Router
""",
    )
    write(
        root / "custom_skills" / "quartz-helper" / "SKILL.md",
        """---
name: quartz-helper
description: Help with Quartz inventory operations.
---
# Quartz Helper
""",
    )
    return Config(
        source_root=root,
        hermes_home=hermes_home,
        state_dir=state_dir,
        index_settings=IndexSettings(
            custom_skill_dirs=("custom_skills",),
            script_dirs=("scripts",),
            memory_dirs=("memory",),
            runbook_dirs=("docs",),
            known_entities=("Quartz",),
            include_markdown_docs=True,
        ),
        source_root_source="config",
        state_dir_source="config",
        include_markdown_docs_source="config",
        warnings=("test warning",),
    )


def test_force_mapping_none_build_and_metadata(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    artifact = Artifact(
        id="skill:quartz-router",
        type="skill",
        title="Quartz Router",
        path="custom_skills/quartz-router/SKILL.md",
        summary="Route Quartz inventory operations.",
    )
    edge = Edge(
        source="skill:quartz-router",
        target="skill:quartz-helper",
        kind="related_to",
        evidence="skill:quartz-helper",
    )
    forces: list[bool] = []

    def build(
        root: Path,
        output_dir: Path,
        hermes_home: Path,
        settings: IndexSettings,
        *,
        force: bool,
    ) -> tuple[list[Artifact], list[Edge]] | None:
        assert (root, output_dir, hermes_home, settings) == (
            config.source_root,
            config.state_dir,
            config.hermes_home,
            config.index_settings,
        )
        forces.append(force)
        return ([artifact], [edge]) if force else None

    service = LocalKnowledgeService(
        config,
        build_index_fn=build,
        index_metadata_fn=lambda path: {
            "index_exists": path == config.state_dir / "index.sqlite"
        },
    )

    db_path, ensure_metadata = service.ensure_index()
    artifacts, edges, rebuild_metadata = service.rebuild()

    assert db_path == config.state_dir / "index.sqlite"
    assert forces == [False, True]
    assert ensure_metadata["rebuilt"] is False
    assert "build_duration_ms" not in ensure_metadata
    assert ensure_metadata["expected_index_format_version"] == index.INDEX_FORMAT_VERSION
    assert ensure_metadata["plugin_version"]
    assert ensure_metadata["root"] == str(config.source_root)
    assert ensure_metadata["state_dir"] == str(config.state_dir)
    assert ensure_metadata["warnings"] == ["test warning"]
    assert artifacts == [artifact]
    assert edges == [edge]
    assert rebuild_metadata["rebuilt"] is True
    assert rebuild_metadata["artifact_count"] == 1
    assert rebuild_metadata["artifact_counts_by_type"] == {"skill": 1}
    assert rebuild_metadata["edge_count"] == 1
    assert "build_duration_ms" in rebuild_metadata


def test_forced_rebuild_rejects_none_result(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    def no_build(*args: Any, **kwargs: Any) -> None:
        return None

    service = LocalKnowledgeService(config, build_index_fn=no_build)

    with pytest.raises(RuntimeError, match="forced index rebuild returned no result"):
        service.rebuild()


@pytest.mark.parametrize("initial_state", ["missing", "older", "corrupt"])
def test_managed_missing_older_and_corrupt_indexes_auto_rebuild(
    tmp_path: Path,
    initial_state: str,
) -> None:
    config = make_config(tmp_path)
    db_path = config.state_dir / "index.sqlite"
    if initial_state == "older":
        config.state_dir.mkdir(parents=True)
        with sqlite3.connect(db_path) as connection:
            connection.execute(f"PRAGMA user_version={index.INDEX_FORMAT_VERSION - 1}")
    elif initial_state == "corrupt":
        write(db_path, "not a sqlite database")

    service = LocalKnowledgeService(config)
    rebuilt_path, metadata = service.ensure_index()
    rows, query_metadata = service.search("quartz inventory", limit=5)

    assert rebuilt_path == db_path
    assert metadata["rebuilt"] is True
    assert query_metadata["rebuilt"] is False
    assert index.index_format_state(db_path) == ("current", index.INDEX_FORMAT_VERSION)
    assert any(row["id"] == "skill:quartz-router" for row in rows)


@pytest.mark.parametrize("operation", ["ensure", "rebuild"])
def test_newer_index_is_refused_before_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    config = make_config(tmp_path)
    config.state_dir.mkdir(parents=True)
    db_path = config.state_dir / "index.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"PRAGMA user_version={index.INDEX_FORMAT_VERSION + 1}")
    before = db_path.read_bytes()

    def fail_scan(*args: Any, **kwargs: Any) -> list[Artifact]:
        raise AssertionError("newer index must be refused before scanning")

    monkeypatch.setattr(index, "collect_artifacts", fail_scan)
    service = LocalKnowledgeService(config)

    with pytest.raises(index.NewerIndexFormatError) as raised:
        service.ensure_index() if operation == "ensure" else service.rebuild()

    assert raised.value.expected_version == index.INDEX_FORMAT_VERSION
    assert raised.value.actual_version == index.INDEX_FORMAT_VERSION + 1
    assert db_path.read_bytes() == before


def test_clean_nonforced_ensure_skips_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    service = LocalKnowledgeService(config)
    service.rebuild()

    def fail_scan(*args: Any, **kwargs: Any) -> list[Artifact]:
        raise AssertionError("clean current index must not be scanned")

    monkeypatch.setattr(index, "collect_artifacts", fail_scan)

    _db_path, metadata = service.ensure_index()

    assert metadata["rebuilt"] is False
    assert metadata["index_format_version"] == index.INDEX_FORMAT_VERSION


def test_dirty_token_triggers_one_builder_owned_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    LocalKnowledgeService(config).rebuild()
    marker = config.state_dir / index.DIRTY_MARKER_NAME
    marker.mkdir(parents=True, exist_ok=True)
    token = marker / "new-okf"
    token.touch()
    original_build = index.build_index
    forces: list[bool] = []

    def counted_build(
        root: Path,
        output_dir: Path,
        hermes_home: Path,
        settings: IndexSettings,
        *,
        force: bool,
    ) -> tuple[list[Artifact], list[Edge]] | None:
        forces.append(force)
        return original_build(root, output_dir, hermes_home, settings, force=force)

    monkeypatch.setattr(index, "build_index", counted_build)
    service = LocalKnowledgeService(config)

    _db_path, metadata = service.ensure_index()

    assert forces == [False]
    assert metadata["rebuilt"] is True
    assert not token.exists()


def test_build_search_get_and_neighbors_outputs(tmp_path: Path) -> None:
    service = LocalKnowledgeService(make_config(tmp_path))

    artifacts, edges, build_metadata = service.rebuild()
    search_rows, search_metadata = service.search("quartz inventory", limit=3)
    artifact, get_metadata = service.get("skill:quartz-router")
    neighbors, neighbors_metadata = service.neighbors("skill:quartz-router")

    assert {item.id for item in artifacts} >= {"skill:quartz-router", "skill:quartz-helper"}
    assert edges
    assert build_metadata["rebuilt"] is True
    assert search_rows[0]["id"] == "skill:quartz-router"
    assert search_metadata["rebuilt"] is False
    assert artifact is not None
    assert artifact["id"] == "skill:quartz-router"
    assert get_metadata["rebuilt"] is False
    assert any(row["id"] == "skill:quartz-helper" for row in neighbors)
    assert neighbors_metadata["rebuilt"] is False


def test_caller_owned_db_never_ensures_or_consumes_managed_tokens(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    caller_state = tmp_path / "caller-state"
    build = index.build_index(
        config.source_root,
        caller_state,
        config.hermes_home,
        config.index_settings,
    )
    assert build is not None
    caller_db = caller_state / "index.sqlite"
    marker = config.state_dir / index.DIRTY_MARKER_NAME
    marker.mkdir(parents=True)
    token = marker / "managed-dirty"
    token.touch()

    def unexpected_build(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("caller-owned DB must not invoke the builder")

    service = LocalKnowledgeService(config, build_index_fn=unexpected_build)

    search_rows, _ = service.search(
        "quartz inventory",
        limit=3,
        rebuild=True,
        db_path=caller_db,
        ensure=True,
    )
    artifact, _ = service.get(
        "skill:quartz-router",
        rebuild=True,
        db_path=caller_db,
        ensure=True,
    )
    neighbors, _ = service.neighbors(
        "skill:quartz-router",
        rebuild=True,
        db_path=caller_db,
        ensure=True,
    )

    assert search_rows
    assert artifact is not None
    assert neighbors
    assert token.is_file()
    assert not (config.state_dir / index.INDEX_BUILD_LOCK_NAME).exists()
    assert not (config.state_dir / index.INDEX_BUILD_TRANSACTION_LOCK_NAME).exists()


def test_query_dependency_injection_and_configured_paths_are_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    calls: list[tuple[Any, ...]] = []

    def search_fn(db_path: Path, query: str, *, limit: int, artifact_type: str | None = None) -> list[dict[str, Any]]:
        calls.append(("search", db_path, query, limit, artifact_type))
        return [{"id": "skill:injected"}]

    def get_fn(db_path: Path, artifact_id: str) -> dict[str, Any]:
        calls.append(("get", db_path, artifact_id))
        return {"id": artifact_id}

    def neighbors_fn(db_path: Path, artifact_id: str) -> list[dict[str, Any]]:
        calls.append(("neighbors", db_path, artifact_id))
        return [{"id": "skill:neighbor"}]

    service = LocalKnowledgeService(
        config,
        search_index_fn=search_fn,
        get_artifact_fn=get_fn,
        get_neighbors_fn=neighbors_fn,
        index_metadata_fn=lambda _path: {"index_exists": True},
    )
    monkeypatch.setenv("LOCAL_KNOWLEDGE_STATE_DIR", str(tmp_path / "changed-state"))

    assert service.db_path == config.state_dir / "index.sqlite"
    assert service.usage_db_path == config.state_dir / "usage.sqlite"
    assert service.search("injected", limit=4, artifact_type="skill", ensure=False)[0] == [
        {"id": "skill:injected"}
    ]
    assert service.get("skill:injected", ensure=False)[0] == {"id": "skill:injected"}
    assert service.neighbors("skill:injected", ensure=False)[0] == [{"id": "skill:neighbor"}]
    assert calls == [
        ("search", service.db_path, "injected", 4, "skill"),
        ("get", service.db_path, "skill:injected"),
        ("neighbors", service.db_path, "skill:injected"),
    ]


def test_usage_success_error_and_failures_are_best_effort(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = LocalKnowledgeService(config)

    success_id = service.record_usage(
        tool="knowledge_search",
        success=True,
        query="quartz inventory",
        result_count=1,
    )
    error_id = service.record_usage(
        tool="knowledge_search",
        success=False,
        query="broken query",
        error="synthetic failure",
    )

    assert isinstance(success_id, int)
    assert isinstance(error_id, int)
    with sqlite3.connect(service.usage_db_path) as connection:
        rows = connection.execute("SELECT success, error FROM usage_events ORDER BY id").fetchall()
    assert rows == [(1, None), (0, "synthetic failure")]

    def fail_record(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("telemetry unavailable")

    failing = LocalKnowledgeService(config, record_usage_fn=fail_record)
    assert failing.record_usage(tool="knowledge_search", success=True) is None
    assert failing.record_usage(tool="knowledge_search", success=False, error="lookup failed") is None


def test_feedback_failures_remain_strict(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    def fail_feedback(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("feedback unavailable")

    service = LocalKnowledgeService(config, record_feedback_fn=fail_feedback)

    with pytest.raises(sqlite3.OperationalError, match="feedback unavailable"):
        service.feedback(
            rating="useful",
            event_id=None,
            query="quartz inventory",
            artifact_id="skill:quartz-router",
            note="",
            context={},
        )


def test_feedback_report_and_evaluation_preserve_paths_and_behavior(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = LocalKnowledgeService(config)
    service.rebuild()
    usage_event_id = service.record_usage(
        tool="knowledge_search",
        success=True,
        query="quartz inventory",
        artifact_id="skill:quartz-router",
        result_count=1,
        top_ids=["skill:quartz-router"],
    )
    assert isinstance(usage_event_id, int)

    feedback_id = service.feedback(
        rating="useful",
        event_id=usage_event_id,
        query="quartz inventory",
        artifact_id="skill:quartz-router",
        note="correct route",
        context={"session_id": "session-1"},
    )
    report = service.usage_report(days=30, limit=10)
    before_evaluation = (
        report["total_events"],
        report["feedback_count"],
    )
    evaluation = service.evaluate()
    after_evaluation = service.usage_report(days=30, limit=10)

    assert isinstance(feedback_id, int)
    assert report["usage_db_path"] == str(service.usage_db_path)
    assert before_evaluation == (1, 1)
    assert evaluation.metrics.query_count == 1
    assert evaluation.metrics.hit_at_1 == 1.0
    assert evaluation.metrics.parent_equiv_hit_at_10 == 1.0
    assert (after_evaluation["total_events"], after_evaluation["feedback_count"]) == before_evaluation


def test_feedback_report_and_evaluation_injections_receive_configured_paths(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    calls: list[tuple[Any, ...]] = []
    expected_report = object()

    def feedback_fn(root: Path, **kwargs: Any) -> int:
        calls.append(("feedback", root, kwargs["usage_db_path"]))
        return 41

    def report_fn(root: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append(("report", root, kwargs["usage_db_path"], kwargs["days"], kwargs["limit"]))
        return {"success": True}

    def evaluate_fn(db_path: Path, usage_db_path: Path) -> Any:
        calls.append(("evaluate", db_path, usage_db_path))
        return expected_report

    service = LocalKnowledgeService(
        config,
        record_feedback_fn=feedback_fn,
        usage_report_fn=report_fn,
        evaluate_fn=evaluate_fn,
    )

    assert service.feedback(
        rating="useful",
        event_id=None,
        query="",
        artifact_id="",
        note="",
        context={},
    ) == 41
    assert service.usage_report(days=7, limit=3) == {"success": True}
    assert service.evaluate() is expected_report
    assert calls == [
        ("feedback", config.source_root, service.usage_db_path),
        ("report", config.source_root, service.usage_db_path, 7, 3),
        ("evaluate", service.db_path, service.usage_db_path),
    ]
