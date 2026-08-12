from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_local_knowledge import index as lci_index
from hermes_local_knowledge import indexer as lci


def load_compare_helper() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "compare_historical_query_versions.py"
    spec = importlib.util.spec_from_file_location("compare_historical_query_versions", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_evaluator() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_ref.py"
    spec = importlib.util.spec_from_file_location("evaluate_ref", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_usage_db(path: Path, live_root: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                tool TEXT NOT NULL,
                query TEXT,
                artifact_id TEXT,
                artifact_type TEXT,
                limit_value INTEGER,
                rebuild_requested INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL,
                result_count INTEGER,
                root TEXT
            );
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                event_id INTEGER,
                rating TEXT NOT NULL,
                query TEXT,
                artifact_id TEXT,
                root TEXT
            );
            """
        )
        root_text = str(live_root.resolve())
        other_root = str(live_root.parent / "other")
        conn.executemany(
            """
            INSERT INTO usage_events (
                id, ts, tool, query, artifact_id, artifact_type, limit_value,
                rebuild_requested, success, result_count, root
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "2026-01-01T00:00:00Z", "knowledge_search", "alpha", None, None, 10, 0, 1, 1, root_text),
                (2, "2026-01-01T00:00:01Z", "knowledge_search", "zero", None, "script", 4, 1, 1, 0, root_text),
                (3, "2026-01-01T00:00:02Z", "knowledge_get", None, "skill:alpha", None, None, 0, 1, 1, root_text),
                (4, "2026-01-01T00:00:03Z", "knowledge_neighbors", None, "skill:alpha", None, 3, 0, 1, 1, root_text),
                (5, "2026-01-01T00:00:04Z", "knowledge_search", "other", None, None, 10, 0, 1, 1, other_root),
                (6, "2026-01-01T00:00:05Z", "knowledge_search", "failed", None, None, 10, 0, 0, 0, root_text),
                (7, "2026-01-01T00:00:06Z", "knowledge_search", "demo", None, None, 10, 0, 1, 0, root_text),
            ],
        )
        conn.executemany(
            """
            INSERT INTO feedback (id, ts, event_id, rating, query, artifact_id, root)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "2026-01-01T00:01:00Z", 1, "useful", None, "skill:alpha", root_text),
                (2, "2026-01-01T00:01:01Z", None, "great", "alpha", "skill:parent", root_text),
                (3, "2026-01-01T00:01:02Z", None, "useful", "stale", "skill:missing", root_text),
                (4, "2026-01-01T00:01:03Z", None, "noisy", "alpha", "skill:bad", root_text),
                (5, "2026-01-01T00:01:04Z", None, "wrong_artifact", "alpha", "skill:missing", root_text),
                (6, "2026-01-01T00:01:05Z", None, "not_useful", "demo", "skill:bad", root_text),
                (7, "2026-01-01T00:01:06Z", 5, "useful", None, "skill:other", other_root),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def create_provenance_usage_db(path: Path, live_root: Path, unrelated_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                tool TEXT NOT NULL,
                query TEXT,
                success INTEGER NOT NULL,
                root TEXT,
                baseline_top_ids_json TEXT NOT NULL DEFAULT '[]',
                top_ids_json TEXT NOT NULL DEFAULT '[]',
                route_feedback_id INTEGER,
                route_artifact_id TEXT,
                feedback_max_id INTEGER
            );
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                event_id INTEGER,
                rating TEXT NOT NULL,
                query TEXT,
                artifact_id TEXT,
                root TEXT,
                expected_artifact_id TEXT,
                resolves_feedback_id INTEGER,
                linkage_status TEXT
            );
            CREATE TABLE index_builds (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                root TEXT
            );
            """
        )
        conn.execute(
            "CREATE TABLE implicit_feedback (id INTEGER PRIMARY KEY, root TEXT)"
        )
        conn.execute(
            "INSERT INTO implicit_feedback (id, root) VALUES (1, ?)",
            (str(live_root),),
        )
        conn.executemany(
            """
            INSERT INTO usage_events (
                id, ts, tool, query, success, root, baseline_top_ids_json,
                top_ids_json, route_feedback_id, route_artifact_id, feedback_max_id
            ) VALUES (?, ?, 'knowledge_search', ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-01-01T00:00:00Z",
                    "alpha",
                    str(live_root),
                    '["skill:old"]',
                    '["skill:old"]',
                    1,
                    "skill:old",
                    1,
                ),
                (
                    2,
                    "2026-01-01T00:01:00Z",
                    "beta",
                    str(live_root),
                    '["skill:old"]',
                    '["skill:new"]',
                    2,
                    "skill:new",
                    2,
                ),
                (
                    3,
                    "2026-01-01T00:02:00Z",
                    "other",
                    str(unrelated_root),
                    "[]",
                    "[]",
                    None,
                    None,
                    3,
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO feedback (
                id, ts, event_id, rating, query, artifact_id, root,
                expected_artifact_id, resolves_feedback_id, linkage_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-01-01T00:00:01Z",
                    1,
                    "not_useful",
                    "alpha",
                    "skill:old",
                    str(live_root),
                    "skill:new",
                    None,
                    "verified_event",
                ),
                (
                    2,
                    "2026-01-01T00:01:01Z",
                    2,
                    "useful",
                    "beta",
                    "skill:new",
                    str(live_root),
                    None,
                    1,
                    "verified_event",
                ),
                (
                    3,
                    "2026-01-01T00:02:01Z",
                    2,
                    "not_useful",
                    "later",
                    "skill:new",
                    str(live_root),
                    None,
                    None,
                    "direct_query",
                ),
                (
                    4,
                    "2026-01-01T00:03:01Z",
                    3,
                    "useful",
                    "other",
                    "skill:other",
                    str(unrelated_root),
                    None,
                    None,
                    "direct_query",
                ),
            ],
        )
        conn.execute(
            "INSERT INTO index_builds (id, ts, root) VALUES (1, '2026-01-01T00:00:00Z', ?)",
            (str(live_root),),
        )


def make_ref_layout(helper: Any, base: Path, name: str) -> Any:
    checkout = base / name / "checkout"
    source_root = base / name / "source"
    home = base / name / "home"
    hermes_home = home / ".hermes"
    state_dir = base / name / "state"
    synthetic_root = base / name / "synthetic" / "source"
    synthetic_home = base / name / "synthetic" / "home"
    synthetic_hermes_home = synthetic_home / ".hermes"
    synthetic_state_dir = base / name / "synthetic" / "state"
    for directory in (
        checkout,
        source_root,
        hermes_home,
        state_dir,
        synthetic_root,
        synthetic_hermes_home,
        synthetic_state_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return helper.RefLayout(
        name,
        name,
        checkout,
        "hermes_local_knowledge.indexer",
        source_root,
        home,
        hermes_home,
        state_dir,
        synthetic_root,
        synthetic_home,
        synthetic_hermes_home,
        synthetic_state_dir,
        {},
        {},
    )


def build_tiny_index(state_dir: Path, *, title: str = "Alpha", search_text: str = "alpha route", edge_evidence: str = "alpha") -> None:
    artifacts = [
        lci.Artifact(
            id="skill:alpha",
            type="skill",
            title=title,
            path="custom_skills/alpha",
            summary="Alpha routing skill.",
            triggers=["alpha", "route"],
            entities=["Hermes"],
            related=["script:alpha"],
            source="custom_skill_source",
            search_text=search_text,
        ),
        lci.Artifact(
            id="script:alpha",
            type="script",
            title="scripts/alpha.py",
            path="scripts/alpha.py",
            summary="Alpha helper.",
            triggers=["alpha"],
            source="repo_script",
            search_text="alpha helper",
        ),
    ]
    edges = [lci.Edge("skill:alpha", "script:alpha", "related_to", edge_evidence)]
    state_dir.mkdir(parents=True, exist_ok=True)
    lci_index._write_jsonl(state_dir / "index.jsonl", artifacts)
    lci_index._build_sqlite(
        state_dir / "index.sqlite",
        artifacts,
        edges,
        source_root=state_dir,
        build_duration_ms=0,
    )


def test_compare_helper_ref_names_are_collision_resistant() -> None:
    helper = load_compare_helper()

    assert helper.safe_ref_name("feature/a") != helper.safe_ref_name("feature-a")


def test_compare_helper_evaluator_environment_is_clean_and_ref_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = load_compare_helper()
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "poison"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "python-home"))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_ROOT", str(tmp_path / "ambient-root"))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_STATE_DIR", str(tmp_path / "ambient-state"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ambient-hermes"))

    checkout = tmp_path / "checkout"
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    source = tmp_path / "source"
    state_dir = tmp_path / "state"
    env = helper.build_child_env(
        checkout,
        home=home,
        hermes_home=hermes_home,
        source_root=source,
        state_dir=state_dir,
        explicit_root=True,
    )

    assert env["PYTHONPATH"] == str(checkout.resolve())
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONHASHSEED"] == "0"
    assert "PYTHONHOME" not in env
    assert env["HOME"] == str(home.resolve())
    assert env["HERMES_HOME"] == str(hermes_home.resolve())
    assert env["LOCAL_KNOWLEDGE_ROOT"] == str(source.resolve())
    assert env["LOCAL_KNOWLEDGE_STATE_DIR"] == str(state_dir.resolve())


def test_tracked_evaluator_rejects_module_outside_intended_ref(tmp_path: Path) -> None:
    evaluator = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_ref.py"
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"action": "evaluate", "case_file": str(tmp_path / "cases.json")}), encoding="utf-8")
    (tmp_path / "cases.json").write_text(json.dumps({"labels": {}, "replay": {}, "synthetic": []}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(evaluator),
            "--request",
            str(request),
            "--ref-root",
            str(tmp_path),
            "--api-module",
            "json",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONHASHSEED": "0"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    payload = json.loads(result.stdout)
    assert result.returncode != 0
    assert payload["ok"] is False
    assert payload["error_type"] == "ModuleProvenanceError"
    assert result.stdout.count("{") == 1


def test_evaluator_prefers_target_config_resolver_and_falls_back_to_pinned_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = load_evaluator()
    package = tmp_path / "demo_package"
    package.mkdir()
    config_resolver = lambda path: ("config", path)  # noqa: E731
    runtime_resolver = lambda path: ("runtime", path)  # noqa: E731
    config_module = SimpleNamespace(__file__=str(package / "config.py"), resolve_config=config_resolver)
    runtime_module = SimpleNamespace(__file__=str(package / "runtime.py"), _runtime_config=runtime_resolver)

    modules = {"demo_package.config": config_module, "demo_package.runtime": runtime_module}
    monkeypatch.setattr(evaluator.importlib, "import_module", modules.__getitem__)
    assert evaluator._config_resolver("demo_package", tmp_path) is config_resolver

    config_without_resolver = SimpleNamespace(__file__=str(package / "config.py"))
    modules["demo_package.config"] = config_without_resolver
    assert evaluator._config_resolver("demo_package", tmp_path) is runtime_resolver


def test_ref_config_validation_rejects_candidate_path_setting_and_provenance_drift(tmp_path: Path) -> None:
    helper = load_compare_helper()
    source = tmp_path / "source"
    state = source / "knowledge"
    hermes = tmp_path / "home" / ".hermes"
    settings = {
        "custom_skill_dirs": ["custom_skills"],
        "script_dirs": ["scripts"],
        "memory_dirs": ["memory"],
        "runbook_dirs": ["docs"],
        "known_entities": ["Hermes"],
        "include_markdown_docs": True,
        "exclude_dir_names": [],
    }
    baseline = {
        "source_root_source": "config",
        "state_dir_source": "config",
        "include_markdown_docs_source": "config",
    }
    resolved = {
        "source_root": str(source),
        "state_dir": str(state),
        "hermes_home": str(hermes),
        "settings": settings,
        **baseline,
    }

    assert helper._validate_ref_config(
        resolved,
        source_root=source,
        state_dir=state,
        hermes_home=hermes,
        settings=settings,
        baseline_config=baseline,
    ) == settings

    for key, value in (
        ("source_root", str(tmp_path / "wrong-source")),
        ("settings", {**settings, "include_markdown_docs": False}),
        ("state_dir_source", "default"),
    ):
        drifted = {**resolved, key: value}
        with pytest.raises(RuntimeError, match="configuration contract mismatch"):
            helper._validate_ref_config(
                drifted,
                source_root=source,
                state_dir=state,
                hermes_home=hermes,
                settings=settings,
                baseline_config=baseline,
            )


def test_evaluator_truncates_historical_order_to_recorded_limit_or_ten() -> None:
    evaluator = load_evaluator()
    outcome: dict[str, Any] = {
        "status": "ok",
        "ids": [f"artifact:{index}" for index in range(20)],
        "duration_ms": 1.0,
    }

    assert len(evaluator._truncate_search_outcome(outcome, 30)["ids"]) == 10
    assert len(evaluator._truncate_search_outcome(outcome, 3)["ids"]) == 3
    assert len(evaluator._truncate_search_outcome(outcome, None)["ids"]) == 10
    assert len(outcome["ids"]) == 20


def test_usage_snapshot_is_read_only_and_private_backup_has_same_counts(tmp_path: Path) -> None:
    helper = load_compare_helper()
    source = tmp_path / "usage.sqlite"
    live_root = tmp_path / "root"
    create_usage_db(source, live_root)
    backup = tmp_path / "private" / "usage.sqlite"

    snapshot = helper.snapshot_usage_database(source, backup)

    assert snapshot.query_only is True
    assert snapshot.before_counts == snapshot.after_counts
    assert helper.sqlite_table_counts(backup) == snapshot.before_counts
    conn = sqlite3.connect(source)
    try:
        conn.execute(
            "INSERT INTO usage_events (id, ts, tool, success, root) VALUES (99, '2026-01-01T00:00:00Z', 'knowledge_get', 1, ?)",
            (str(live_root.resolve()),),
        )
        conn.commit()
    finally:
        conn.close()


def test_prepared_production_states_are_immutable_bounded_and_root_exact(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "live_root"
    # A descendant path can be an independently configured production root.
    unrelated_root = live_root / "nested"
    source = tmp_path / "live-usage.sqlite"
    create_provenance_usage_db(source, live_root, unrelated_root)
    frozen = helper.snapshot_usage_database(source, tmp_path / "frozen" / "usage.sqlite")
    frozen_hash = helper._sha256_file(frozen.backup_path)
    baseline = make_ref_layout(helper, tmp_path / "refs", "baseline")
    candidate = make_ref_layout(helper, tmp_path / "refs", "candidate")
    build_tiny_index(baseline.state_dir)
    build_tiny_index(candidate.state_dir)
    cases = (
        helper.ReplaySearchCase("unavailable", "alpha", 10, None, False, feedback_max_id=None),
        helper.ReplaySearchCase("legacy", "alpha", 10, None, False, feedback_max_id=-1),
        helper.ReplaySearchCase("bound-zero", "alpha", 10, None, False, feedback_max_id=0),
        helper.ReplaySearchCase("bound-two", "beta", 10, None, False, feedback_max_id=2),
    )

    baseline_states, baseline_evidence = helper._prepare_production_states(
        baseline, frozen.backup_path, live_root, cases
    )
    candidate_states, candidate_evidence = helper._prepare_production_states(
        candidate, frozen.backup_path, live_root, cases
    )

    assert set(baseline_states) == {"unavailable", "legacy", "bound-0", "bound-2"}
    assert not (Path(baseline_states["unavailable"]) / "usage.sqlite").exists()
    assert baseline_evidence["unavailable"] == {
        "feedback_max_id": None,
        "feedback_bound": None,
        "feedback_bound_available": False,
        "usage_sha256": None,
        "canonical_usage_sha256": None,
        "table_counts": {},
        "link_counts": {},
    }
    assert baseline_evidence["legacy"]["feedback_bound_available"] is False
    assert baseline_evidence["bound-0"]["feedback_bound_available"] is True
    assert baseline_evidence["bound-2"]["feedback_bound_available"] is True
    assert {
        key: value["canonical_usage_sha256"] for key, value in baseline_evidence.items()
    } == {
        key: value["canonical_usage_sha256"] for key, value in candidate_evidence.items()
    }
    assert {
        key: value["link_counts"] for key, value in baseline_evidence.items()
    } == {
        key: value["link_counts"] for key, value in candidate_evidence.items()
    }

    with sqlite3.connect(Path(baseline_states["bound-0"]) / "usage.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone() == (3,)
        assert conn.execute("SELECT id FROM feedback ORDER BY id").fetchall() == [(4,)]
        assert conn.execute("SELECT COUNT(*) FROM implicit_feedback").fetchone() == (0,)
    with sqlite3.connect(Path(baseline_states["bound-2"]) / "usage.sqlite") as conn:
        assert conn.execute("SELECT id FROM usage_events ORDER BY id").fetchall() == [(1,), (2,), (3,)]
        assert conn.execute("SELECT id FROM feedback ORDER BY id").fetchall() == [(1,), (2,), (4,)]
        assert conn.execute("SELECT root FROM usage_events WHERE id=1").fetchone() == (
            str(baseline.source_root),
        )
        assert conn.execute("SELECT root FROM usage_events WHERE id=2").fetchone() == (
            str(baseline.source_root),
        )
        assert conn.execute("SELECT root FROM usage_events WHERE id=3").fetchone() == (
            str(unrelated_root),
        )
        assert conn.execute("SELECT root FROM index_builds WHERE id=1").fetchone() == (
            str(baseline.source_root),
        )
    with sqlite3.connect(Path(candidate_states["bound-2"]) / "usage.sqlite") as conn:
        assert conn.execute("SELECT root FROM usage_events WHERE id=1").fetchone() == (
            str(candidate.source_root),
        )
        assert conn.execute("SELECT root FROM usage_events WHERE id=3").fetchone() == (
            str(unrelated_root),
        )

    assert baseline_evidence["bound-2"]["link_counts"] == {
        "feedback_event_rows": 3,
        "feedback_event_links": 3,
        "feedback_resolution_rows": 1,
        "feedback_resolution_links": 1,
        "usage_route_rows": 2,
        "usage_route_links": 2,
    }
    assert helper._sha256_file(frozen.backup_path) == frozen_hash


def test_canonical_usage_digest_does_not_hide_descendant_root_changes(tmp_path: Path) -> None:
    helper = load_compare_helper()
    left_root = tmp_path / "left"
    right_root = tmp_path / "right"
    left_db = tmp_path / "left.sqlite"
    right_db = tmp_path / "right.sqlite"
    for path, root in ((left_db, left_root), (right_db, right_root)):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE usage_events (id INTEGER PRIMARY KEY, root TEXT)")
            conn.execute("INSERT INTO usage_events VALUES (1, ?)", (str(root / "nested"),))

    assert helper._canonical_usage_digest(left_db, root=left_root) != helper._canonical_usage_digest(
        right_db,
        root=right_root,
    )


def test_prepared_bound_is_compatible_with_unmodified_v042_schema(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "root"
    frozen = tmp_path / "frozen.sqlite"
    create_usage_db(frozen, live_root)
    layout = make_ref_layout(helper, tmp_path / "refs", "v0.4.2")
    build_tiny_index(layout.state_dir)
    cases = (helper.ReplaySearchCase("bounded", "alpha", 10, None, False, feedback_max_id=1),)

    states, evidence = helper._prepare_production_states(layout, frozen, live_root, cases)
    working_db = Path(states["bound-1"]) / "usage.sqlite"

    with sqlite3.connect(working_db) as conn:
        assert conn.execute("SELECT id FROM feedback ORDER BY id").fetchall() == [(1,), (7,)]
        assert conn.execute("SELECT id FROM usage_events ORDER BY id").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
        ]
        feedback_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(feedback)")}
        usage_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(usage_events)")}
    assert "resolves_feedback_id" not in feedback_columns
    assert "feedback_max_id" not in usage_columns
    assert evidence["bound-1"]["feedback_bound_available"] is True


def test_missing_positive_feedback_boundary_is_not_exact(tmp_path: Path) -> None:
    helper = load_compare_helper()
    usage_db = tmp_path / "usage.sqlite"
    root = tmp_path / "root"
    with sqlite3.connect(usage_db) as conn:
        conn.execute("CREATE TABLE feedback (id INTEGER PRIMARY KEY, root TEXT)")
        conn.executemany(
            "INSERT INTO feedback (id, root) VALUES (?, ?)",
            [(1, str(root)), (2, str(tmp_path / "other")), (3, str(root))],
        )
        assert helper._delete_feedback_after_bound(
            conn,
            feedback_max_id=2,
            root=root,
        ) is False


def test_production_evidence_summary_separates_exact_and_best_effort_classes() -> None:
    helper = load_compare_helper()

    assert helper._production_evidence_summary(
        {
            "exact": {"status": "ok", "event_time_exact": True, "feedback_bound_kind": "bound-2"},
            "counterfactual": {
                "status": "ok",
                "event_time_exact": False,
                "event_inputs_exact": True,
                "feedback_bound_kind": "bound-2",
            },
            "legacy": {"status": "ok", "event_time_exact": False, "feedback_bound_kind": "legacy", "corpus_match": None},
            "unavailable": {"status": "ok", "event_time_exact": False, "feedback_bound_kind": "unavailable", "corpus_match": None},
            "mismatch": {"status": "ok", "event_time_exact": False, "feedback_bound_kind": "bound-5", "corpus_match": False},
            "unverified": {"status": "ok", "event_time_exact": False, "feedback_bound_kind": "bound-6", "corpus_match": None},
            "error": {"status": "error", "error": "boom"},
        }
    ) == {
        "event_time_exact": 1,
        "exact_input_counterfactual": 1,
        "fixed_capture_legacy": 1,
        "feedback_unavailable": 1,
        "bounded_input_mismatch": 1,
        "bounded_ref_unverified": 1,
        "errors": 1,
    }


def test_production_corpus_match_uses_companion_jsonl_not_sqlite_bytes(tmp_path: Path) -> None:
    evaluator = load_evaluator()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    jsonl = state_dir / "index.jsonl"
    sqlite_file = state_dir / "index.sqlite"
    jsonl.write_bytes(b'{"id":"skill:alpha"}\n')
    sqlite_file.write_bytes(b"sqlite-build-one")
    recorded_hash = evaluator._sha256_file(jsonl)

    class FakeService:
        config = SimpleNamespace(state_dir=state_dir)

        @staticmethod
        def search(*_args: Any, **_kwargs: Any) -> tuple[list[dict[str, str]], dict[str, str]]:
            return [{"id": "skill:alpha"}], {"format_version": "4"}

    row = {
        "query": "alpha",
        "limit": 10,
        "artifact_type": None,
        "index_jsonl_sha256": recorded_hash,
        "feedback_max_id": 0,
    }
    first = evaluator._production_search(
        FakeService(), SimpleNamespace(), row, feedback_bound_available=True
    )
    sqlite_file.write_bytes(b"sqlite-build-two")
    second = evaluator._production_search(
        FakeService(), SimpleNamespace(), row, feedback_bound_available=True
    )

    assert first["corpus_match"] is True
    assert second["corpus_match"] is True
    assert second["corpus_match_basis"] == "index_jsonl_sha256"
    assert second["event_inputs_exact"] is True
    assert second["event_time_exact"] is False
    assert second["plugin_version_match"] is None
    assert second["index_format_match"] is None

    jsonl.write_bytes(b'{"id":"skill:beta"}\n')
    changed = evaluator._production_search(
        FakeService(), SimpleNamespace(), row, feedback_bound_available=True
    )
    assert changed["corpus_match"] is False
    assert changed["event_time_exact"] is False

    current_hash = evaluator._sha256_file(jsonl)
    for bound, state_key in ((None, "unavailable"), (-1, "legacy")):
        legacy = evaluator._production_search(
            FakeService(),
            SimpleNamespace(),
            {**row, "index_jsonl_sha256": current_hash, "feedback_max_id": bound},
        )
        assert legacy["corpus_match"] is True
        assert legacy["event_time_exact"] is False
        assert legacy["feedback_bound_kind"] == state_key
        assert legacy["route_outcome"] is None
        assert legacy["route_feedback_id"] is None


def test_label_exclusions_apply_to_queries_and_artifact_ids(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "root"
    usage_db = tmp_path / "usage.sqlite"
    create_usage_db(usage_db, live_root)
    ignored = ("", "none", "null", "xxxx", "sentinel unlikely", "demo")
    root_text = str(live_root.resolve())
    rows: list[tuple[Any, ...]] = []
    row_id = 20
    for rating, valid_query in (("useful", "blocked-positive"), ("noisy", "blocked-negative")):
        for value in ignored:
            rows.append((row_id, "2026-01-01T00:03:00Z", None, rating, value, "skill:alpha", root_text))
            row_id += 1
            rows.append((row_id, "2026-01-01T00:03:00Z", None, rating, valid_query, value, root_text))
            row_id += 1
    with sqlite3.connect(usage_db) as conn:
        conn.executemany(
            "INSERT INTO feedback (id, ts, event_id, rating, query, artifact_id, root) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    raw = helper.read_usage_corpus(usage_db, live_root.resolve())

    for rows_for_rating in (raw.positive_rows, raw.negative_rows):
        assert all(query.lower() not in ignored for query, _artifact_id in rows_for_rating)
        assert all(artifact_id.lower() not in ignored for _query, artifact_id in rows_for_rating)
    assert not any(query == "blocked-positive" for query, _artifact_id in raw.positive_rows)
    assert not any(query == "blocked-negative" for query, _artifact_id in raw.negative_rows)


def test_frozen_labels_are_live_root_scoped_and_baseline_id_stable(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "root"
    usage_db = tmp_path / "usage.sqlite"
    create_usage_db(usage_db, live_root)
    root_text = str(live_root.resolve())
    with sqlite3.connect(usage_db) as conn:
        conn.executemany(
            """
            INSERT INTO usage_events (
                id, ts, tool, query, artifact_id, artifact_type, limit_value,
                rebuild_requested, success, result_count, root
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (8, "2026-01-01T00:02:00Z", "knowledge_search", "alpha", None, None, 10, 1, 1, 1, root_text),
                (9, "2026-01-01T00:02:01Z", "knowledge_get", None, "skill:alpha", None, None, 1, 1, 1, root_text),
                (10, "2026-01-01T00:02:02Z", "knowledge_neighbors", None, "skill:alpha", None, 3, 1, 1, 1, root_text),
            ],
        )

    raw = helper.read_usage_corpus(usage_db, live_root.resolve())
    frozen = helper.freeze_usage_corpus(raw, {"skill:alpha", "skill:parent", "skill:bad"})

    assert frozen.positive_labels == {"alpha": {"skill:alpha", "skill:parent"}}
    assert frozen.negative_pairs == (("alpha", "skill:bad"),)
    assert len(frozen.replay_search) == 4
    alpha_cases = [case for case in frozen.replay_search if case.query == "alpha"]
    assert {case.event_id for case in alpha_cases} == {1, 8}
    assert {case.rebuild_requested for case in alpha_cases} == {False, True}
    assert len(frozen.replay_get) == 1
    assert frozen.replay_get[0].rebuild_requested is True
    assert len(frozen.replay_neighbors) == 1
    assert frozen.replay_neighbors[0].rebuild_requested is True

    candidate = helper.compute_positive_evaluation(
        frozen.positive_labels,
        {"alpha": []},
        parent_equivalents={},
    )
    assert candidate.metrics["query_count"] == 1
    assert candidate.metrics["label_count"] == 2
    assert candidate.cases[0]["exact_rank"] is None


def test_historical_loader_preserves_legacy_empty_query_exclusion(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "live"
    usage_db = tmp_path / "usage.sqlite"
    create_usage_db(usage_db, live_root)
    with sqlite3.connect(usage_db) as conn:
        conn.execute("DELETE FROM feedback WHERE id <> 1")
        conn.execute("UPDATE feedback SET query = '' WHERE id = 1")

    raw = helper.read_usage_corpus(usage_db, live_root)

    assert raw.positive_rows == ()


def test_historical_search_fallback_identity_includes_rebuild_request(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "live"
    usage_db = tmp_path / "usage.sqlite"
    with sqlite3.connect(usage_db) as conn:
        conn.executescript(
            """
            CREATE TABLE usage_events (
                id INTEGER,
                tool TEXT,
                query TEXT,
                artifact_id TEXT,
                artifact_type TEXT,
                limit_value INTEGER,
                rebuild_requested INTEGER,
                success INTEGER,
                root TEXT
            );
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY,
                event_id INTEGER,
                rating TEXT,
                query TEXT,
                artifact_id TEXT,
                root TEXT
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO usage_events (
                id, tool, query, artifact_id, artifact_type,
                limit_value, rebuild_requested, success, root
            ) VALUES (NULL, 'knowledge_search', 'same query', NULL, NULL, 10, ?, 1, ?)
            """,
            [(0, str(live_root)), (1, str(live_root))],
        )

    raw = helper.read_usage_corpus(usage_db, live_root)

    assert len(raw.replay_search) == 2
    assert {case.rebuild_requested for case in raw.replay_search} == {False, True}


def test_historical_loader_keeps_query_level_route_vetoes(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "live"
    usage_db = tmp_path / "usage.sqlite"
    create_provenance_usage_db(usage_db, live_root, tmp_path / "other")
    with sqlite3.connect(usage_db) as conn:
        conn.execute(
            "INSERT INTO feedback (id, ts, rating, query, artifact_id, root, linkage_status) VALUES (20, '2026-01-01T00:02:00Z', 'not_useful', 'alpha', NULL, ?, 'direct_query')",
            (str(live_root),),
        )

    raw = helper.read_usage_corpus(usage_db, live_root)

    assert {"query": "alpha", "feedback_id": 20, "artifact_id": ""} in raw.route_vetoes


def test_historical_explicit_resolution_allows_parent_without_predeclared_target(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "live"
    usage_db = tmp_path / "usage.sqlite"
    create_provenance_usage_db(usage_db, live_root, tmp_path / "other")
    with sqlite3.connect(usage_db) as conn:
        conn.execute("UPDATE usage_events SET root = ? WHERE id = 2", (str(live_root),))
        conn.execute("UPDATE feedback SET root = ? WHERE id = 2", (str(live_root),))
        conn.execute("UPDATE feedback SET expected_artifact_id = NULL WHERE id = 1")

    raw = helper.read_usage_corpus(usage_db, live_root)

    assert raw.quality_labels["explicit_resolution"] == {"alpha": {"skill:new"}}


def test_historical_explicit_resolution_requires_expected_target_match(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "live"
    usage_db = tmp_path / "usage.sqlite"
    create_provenance_usage_db(usage_db, live_root, tmp_path / "other")
    with sqlite3.connect(usage_db) as conn:
        conn.execute("UPDATE usage_events SET root = ? WHERE id = 2", (str(live_root),))
        conn.execute("UPDATE feedback SET root = ? WHERE id = 2", (str(live_root),))

    raw = helper.read_usage_corpus(usage_db, live_root)
    assert raw.quality_labels["explicit_resolution"] == {"alpha": {"skill:new"}}

    with sqlite3.connect(usage_db) as conn:
        conn.execute(
            "UPDATE feedback SET expected_artifact_id = 'skill:different' WHERE id = 1"
        )
    malformed = helper.read_usage_corpus(usage_db, live_root)
    assert malformed.quality_labels["explicit_resolution"] == {}


def test_historical_explicit_resolution_requires_verified_unique_parent_link(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "live"
    usage_db = tmp_path / "usage.sqlite"
    create_provenance_usage_db(usage_db, live_root, tmp_path / "other")
    with sqlite3.connect(usage_db) as conn:
        conn.execute("UPDATE feedback SET linkage_status = 'legacy' WHERE id = 1")

    unverified = helper.read_usage_corpus(usage_db, live_root)
    assert unverified.quality_labels["explicit_resolution"] == {}

    duplicate_db = tmp_path / "duplicate-usage.sqlite"
    create_provenance_usage_db(duplicate_db, live_root, tmp_path / "other")
    with sqlite3.connect(duplicate_db) as conn:
        conn.execute(
            """
            INSERT INTO feedback (
                id, ts, event_id, rating, query, artifact_id, root,
                expected_artifact_id, resolves_feedback_id, linkage_status
            ) VALUES (5, '2026-01-01T00:04:01Z', 2, 'useful', 'beta', 'skill:new', ?, NULL, 1, 'verified_event')
            """,
            (str(live_root),),
        )

    duplicate = helper.read_usage_corpus(duplicate_db, live_root)
    assert duplicate.quality_labels["explicit_resolution"] == {}


def test_case_file_payload_materializes_quality_only_queries() -> None:
    helper = load_compare_helper()
    frozen = helper.FrozenUsageCorpus(
        positive_labels={"aggregate query": {"skill:aggregate"}},
        negative_pairs=(),
        replay_search=(),
        replay_get=(),
        replay_neighbors=(),
        quality_labels={
            "explicit_resolution": {"correction trigger": {"skill:target"}},
            "verified_event": {},
            "direct_or_legacy": {},
        },
    )

    payload = helper._case_file_payload(frozen, ())

    assert payload["labels"]["quality"]["explicit_resolution"] == [
        {
            "query_id": helper.sha256_text("correction trigger"),
            "query": "correction trigger",
            "accepted_ids": ["skill:target"],
        }
    ]


def test_label_results_retain_quality_only_query_outcomes() -> None:
    helper = load_compare_helper()
    query = "quality-only correction"
    frozen = helper.FrozenUsageCorpus(
        {}, (), (), (), (),
        {"direct_or_legacy": {}, "verified_event": {}, "explicit_resolution": {query: {"skill:target"}}},
        (),
    )
    output = {
        "label_search": {
            helper.sha256_text(query): {"status": "ok", "ids": ["skill:target"]}
        }
    }

    results, errors = helper._label_results(frozen, output)

    assert results == {query: ["skill:target"]}
    assert errors == set()


def test_historical_great_rating_is_aggregate_only(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_root = tmp_path / "live"
    usage_db = tmp_path / "usage.sqlite"
    create_provenance_usage_db(usage_db, live_root, tmp_path / "other")
    with sqlite3.connect(usage_db) as conn:
        conn.execute("UPDATE usage_events SET root = ? WHERE id = 2", (str(live_root),))
        conn.execute("UPDATE feedback SET root = ?, rating = 'great' WHERE id = 2", (str(live_root),))
    raw = helper.read_usage_corpus(usage_db, live_root)
    assert ("beta", "skill:new") in raw.positive_rows
    assert raw.quality_labels["explicit_resolution"] == {}


def test_positive_and_negative_rank_infinity_semantics() -> None:
    helper = load_compare_helper()
    positive = helper.compute_positive_evaluation(
        {"q": {"accepted-a", "accepted-b"}},
        {"q": ["noise", "accepted-b", *[f"tail-{index}" for index in range(9)], "accepted-a"]},
        parent_equivalents={},
    )
    absent = helper.compute_positive_evaluation(
        {"q": {"accepted"}},
        {"q": [*(f"noise-{index}" for index in range(10)), "accepted"]},
        parent_equivalents={},
    )
    negative = helper.compute_negative_evaluation(
        (("q", "bad-at-two"), ("q", "absent")),
        {"q": ["good", "bad-at-two"]},
    )

    assert positive.cases[0]["exact_rank"] == 2
    assert absent.cases[0]["exact_rank"] is None
    assert absent.metrics["hit_at_10"] == 0.0
    assert negative.cases[0]["rank"] == 2
    assert negative.cases[1]["rank"] is None
    assert negative.metrics["pair_count"] == 2
    assert negative.metrics["bad_hit_at_10"] == 0.5


def test_parent_equivalence_is_frozen_to_one_unambiguous_owner() -> None:
    helper = load_compare_helper()
    artifacts = {
        "skill:parent": {"id": "skill:parent", "type": "skill", "related": []},
        "skill:peer": {"id": "skill:peer", "type": "skill", "related": ["skill:parent"]},
        "skill_support_doc:owned": {
            "id": "skill_support_doc:owned",
            "type": "skill_support_doc",
            "related": ["skill:parent"],
        },
        "skill_support_doc:ambiguous": {
            "id": "skill_support_doc:ambiguous",
            "type": "skill_support_doc",
            "related": ["skill:parent", "skill:peer"],
        },
    }

    equivalents = helper.parent_equivalence_map(artifacts)

    assert equivalents["skill:parent"] == {"skill_support_doc:owned"}
    assert equivalents["skill_support_doc:owned"] == {"skill:parent"}
    assert "skill:peer" not in equivalents
    assert "skill_support_doc:ambiguous" not in equivalents


def test_structure_oracle_reports_exact_artifact_fts_and_edge_diffs(tmp_path: Path) -> None:
    helper = load_compare_helper()
    baseline_dir = tmp_path / "baseline"
    artifact_dir = tmp_path / "artifact"
    fts_dir = tmp_path / "fts"
    edge_dir = tmp_path / "edge"
    build_tiny_index(baseline_dir)
    build_tiny_index(artifact_dir, title="Changed alpha")
    build_tiny_index(fts_dir, search_text="changed fts only")
    build_tiny_index(edge_dir, edge_evidence="changed evidence")

    baseline = helper.inspect_index(baseline_dir)
    artifact_diff = helper.compare_index_oracles(baseline, helper.inspect_index(artifact_dir))
    fts_diff = helper.compare_index_oracles(baseline, helper.inspect_index(fts_dir))
    edge_diff = helper.compare_index_oracles(baseline, helper.inspect_index(edge_dir))

    assert baseline.valid is True
    assert artifact_diff["changed_artifact_fields"][0]["fields"] == ["title"]
    assert fts_diff["changed_fts_ids"]
    assert edge_diff["removed_edge_hashes"] and edge_diff["added_edge_hashes"]
    assert artifact_diff["equal"] is False
    assert fts_diff["equal"] is False
    assert edge_diff["equal"] is False


def test_structure_oracle_rejects_dangling_edges_and_bad_json_field_types(tmp_path: Path) -> None:
    helper = load_compare_helper()
    state_dir = tmp_path / "state"
    build_tiny_index(state_dir)
    conn = sqlite3.connect(state_dir / "index.sqlite")
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO edges (source, target, kind, evidence) VALUES ('skill:alpha', 'skill:missing', 'related_to', 'missing')"
        )
        conn.execute("UPDATE artifacts SET triggers_json = '[1]' WHERE id = 'skill:alpha'")
        conn.commit()
    finally:
        conn.close()

    oracle = helper.inspect_index(state_dir)

    assert oracle.valid is False
    assert "dangling_edges" in oracle.errors
    assert "invalid_artifact_json_fields" in oracle.errors


def test_replay_comparison_reports_order_zero_nonempty_and_error_cases() -> None:
    helper = load_compare_helper()
    baseline = {
        "search": {
            "same": {"status": "ok", "ids": ["a", "b"]},
            "zero": {"status": "ok", "ids": []},
            "nonempty": {"status": "ok", "ids": ["a"]},
        },
        "get": {"get": {"status": "ok", "payload": {"id": "a", "triggers": []}}},
        "neighbors": {"neighbors": {"status": "ok", "payload": [{"id": "b", "edge_kind": "related_to"}]}},
    }
    candidate = {
        "search": {
            "same": {"status": "ok", "ids": ["b", "a"]},
            "zero": {"status": "ok", "ids": ["new"]},
            "nonempty": {"status": "error", "error_type": "RuntimeError"},
        },
        "get": {"get": {"status": "ok", "payload": None}},
        "neighbors": {"neighbors": {"status": "ok", "payload": []}},
    }

    diff = helper.compare_replay_outputs(baseline, candidate)

    assert diff["accepted"] is False
    assert diff["order_changes"] == ["same"]
    assert diff["new_nonempty"] == ["zero"]
    assert diff["candidate_errors"] == ["nonempty"]
    assert diff["get_changes"] == ["get"]
    assert diff["neighbor_changes"] == ["neighbors"]


def test_labeled_order_waiver_applies_only_to_exact_unfiltered_top_ten_tuple() -> None:
    helper = load_compare_helper()
    cases = (
        helper.ReplaySearchCase("exact", "same query", 10, None, False),
        helper.ReplaySearchCase("default", "same query", None, None, False),
        helper.ReplaySearchCase("filtered", "same query", 10, "script", False),
        helper.ReplaySearchCase("short", "same query", 5, None, False),
    )

    accepted = helper._accepted_labeled_order_changes(
        ["exact", "default", "filtered", "short"],
        cases,
        {helper.sha256_text("same query")},
    )

    assert accepted == ["default", "exact"]


@pytest.mark.parametrize(
    ("feedback_max_id", "expected"),
    [
        (None, None),
        (0, None),
        (10, (10, "skill:first")),
        (19, (10, "skill:first")),
        (20, (20, "skill:current")),
        (-1, (20, "skill:current")),
    ],
)
def test_current_explicit_route_respects_event_time_and_latest_target(
    feedback_max_id: int | None, expected: tuple[int, str] | None
) -> None:
    helper = load_compare_helper()
    case = helper.ReplaySearchCase(
        "case", "repair", 10, None, False, feedback_max_id=feedback_max_id
    )
    history = {"repair": [(10, "skill:first"), (20, "skill:current")]}

    assert helper._current_explicit_route(case, history) == expected


def test_current_explicit_route_applies_later_query_and_target_vetoes() -> None:
    helper = load_compare_helper()
    case = helper.ReplaySearchCase(
        "case", "  REPAIR  ", 10, None, False, feedback_max_id=30
    )
    history = {"repair": [(10, "skill:first"), (20, "skill:current")]}

    assert helper._current_explicit_route(
        case, history, {"terms:repair": [(25, "skill:current")]}
    ) == (10, "skill:first")
    assert helper._current_explicit_route(
        case, history, {"terms:repair": [(25, "")]}
    ) is None


def test_current_explicit_route_uses_production_matching_and_type_filter() -> None:
    helper = load_compare_helper()
    history = {"backup restore runbook": [(20, "skill:target")]}
    artifacts = {"skill:target": {"type": "skill"}}

    assert helper._current_explicit_route(
        helper.ReplaySearchCase("skill", "backup restore runbook procedure", 10, "skill", False),
        history,
        artifacts=artifacts,
    ) == (20, "skill:target")
    assert helper._current_explicit_route(
        helper.ReplaySearchCase("script", "backup restore runbook procedure", 10, "script", False),
        history,
        artifacts=artifacts,
    ) is None


def test_comparison_assessment_requires_high_confidence_or_validated_route_change() -> None:
    helper = load_compare_helper()

    direct_only = {
        "direct_or_legacy": ["query-hash"],
        "verified_event": [],
        "explicit_resolution": [],
    }
    verified = {**direct_only, "verified_event": ["verified-query-hash"]}

    assert helper._comparison_assessment(
        accepted=False,
        accepted_production_changes=(),
        quality_improvements=direct_only,
    ) == "rejected"
    assert helper._comparison_assessment(
        accepted=True,
        accepted_production_changes=(),
        quality_improvements=direct_only,
    ) == "accepted_unchanged_or_insufficient_evidence"
    assert helper._comparison_assessment(
        accepted=True,
        accepted_production_changes=(),
        quality_improvements=verified,
    ) == "accepted_improved"
    assert helper._comparison_assessment(
        accepted=True,
        accepted_production_changes=("case-hash",),
        quality_improvements=direct_only,
    ) == "accepted_improved"


def test_production_order_waiver_requires_the_recorded_explicit_correction_route() -> None:
    helper = load_compare_helper()
    query = "correct alpha"
    target = "skill:target"
    case_id = "correction-case"
    oracle = helper.IndexOracle(True, (), {}, {}, {}, set(), {})
    positive = helper.compute_positive_evaluation(
        {query: {target}}, {query: [target]}, parent_equivalents={}
    )
    negative = helper.compute_negative_evaluation((), {})
    quality = {
        tier: helper.compute_positive_evaluation(
            {query: {target}} if tier == "explicit_resolution" else {},
            {query: [target]},
            parent_equivalents={},
        )
        for tier in helper.LABEL_QUALITY_TIERS
    }
    raw_replay = {
        "search": {case_id: {"status": "ok", "ids": ["skill:old", target]}},
        "get": {},
        "neighbors": {},
    }
    baseline_production = {
        case_id: {
            "status": "ok",
            "ids": ["skill:old", target],
            "route_outcome": "none",
            "route_feedback_id": None,
            "route_artifact_id": None,
        }
    }
    frozen = helper.FrozenUsageCorpus(
        {query: {target}},
        (),
        (helper.ReplaySearchCase(case_id, query, 10, None, False, feedback_max_id=12),),
        (),
        (),
        {"explicit_resolution": {query: {target}}},
        (
            {
                "query": query,
                "quality_tier": "explicit_resolution",
                "feedback_id": 12,
                "artifact_id": target,
            },
        ),
    )

    def evaluation(ref: str, production: dict[str, dict[str, Any]]) -> Any:
        return helper.RefEvaluation(
            ref,
            ref,
            "hermes_local_knowledge.indexer",
            oracle,
            positive,
            negative,
            raw_replay,
            production,
            {"failed": 0},
            {},
            quality,
        )

    baseline = evaluation("baseline", baseline_production)
    unresolved_candidate = evaluation("unresolved", baseline_production)
    routed_candidate = evaluation(
        "routed",
        {
            case_id: {
                "status": "ok",
                "ids": [target, "skill:old"],
                "route_outcome": "promoted_existing",
                "route_feedback_id": 12,
                "route_artifact_id": target,
            }
        },
    )
    unrelated_candidate = evaluation(
        "unrelated",
        {
            case_id: {
                "status": "ok",
                "ids": [target, "skill:old"],
                "route_outcome": "none",
                "route_feedback_id": None,
                "route_artifact_id": None,
            }
        },
    )

    unresolved = helper._candidate_comparison(baseline, unresolved_candidate, frozen)
    routed = helper._candidate_comparison(baseline, routed_candidate, frozen)
    unrelated = helper._candidate_comparison(baseline, unrelated_candidate, frozen)
    empty_frozen = helper.FrozenUsageCorpus(
        {},
        (),
        (),
        (),
        (),
        {tier: {} for tier in helper.LABEL_QUALITY_TIERS},
        (),
    )
    unchanged = helper._candidate_comparison(baseline, baseline, empty_frozen)

    assert unchanged["accepted"] is True
    assert unchanged["assessment"] == "accepted_unchanged_or_insufficient_evidence"
    assert unresolved["accepted"] is False
    assert unresolved["assessment"] == "rejected"
    assert unresolved["production_replay"]["explicit_rank_one_failures"] == [case_id]
    assert routed["accepted"] is True
    assert routed["assessment"] == "accepted_improved"
    assert routed["production_replay"]["accepted_order_changes"] == [case_id]
    assert unrelated["accepted"] is False
    assert unrelated["assessment"] == "rejected"
    assert unrelated["production_replay"]["unaccepted_order_changes"] == [case_id]


def test_raw_replay_order_is_not_waived_by_an_explicit_production_correction() -> None:
    helper = load_compare_helper()
    query = "correct alpha"
    target = "skill:target"
    case_id = "correction-case"
    oracle = helper.IndexOracle(True, (), {}, {}, {}, set(), {})
    negative = helper.compute_negative_evaluation((), {})
    empty_quality = {
        tier: helper.compute_positive_evaluation({}, {}, parent_equivalents={})
        for tier in helper.LABEL_QUALITY_TIERS
    }
    baseline_positive = helper.compute_positive_evaluation(
        {query: {target}}, {query: ["skill:old", target]}, parent_equivalents={}
    )
    candidate_positive = helper.compute_positive_evaluation(
        {query: {target}}, {query: [target, "skill:old"]}, parent_equivalents={}
    )
    frozen = helper.FrozenUsageCorpus(
        {query: {target}},
        (),
        (helper.ReplaySearchCase(case_id, query, 10, None, False, feedback_max_id=12),),
        (),
        (),
        {"explicit_resolution": {query: {target}}},
        (
            {
                "query": query,
                "quality_tier": "explicit_resolution",
                "feedback_id": 12,
                "artifact_id": target,
            },
        ),
    )

    def evaluation(
        ref: str,
        positive: Any,
        raw_ids: list[str],
        production_ids: list[str],
        *,
        routed: bool,
    ) -> Any:
        return helper.RefEvaluation(
            ref,
            ref,
            "hermes_local_knowledge.indexer",
            oracle,
            positive,
            negative,
            {"search": {case_id: {"status": "ok", "ids": raw_ids}}, "get": {}, "neighbors": {}},
            {
                case_id: {
                    "status": "ok",
                    "ids": production_ids,
                    "route_outcome": "promoted_existing" if routed else "none",
                    "route_feedback_id": 12 if routed else None,
                    "route_artifact_id": target if routed else None,
                }
            },
            {"failed": 0},
            {},
            empty_quality,
        )

    baseline = evaluation(
        "baseline",
        baseline_positive,
        ["skill:old", target],
        ["skill:old", target],
        routed=False,
    )
    candidate = evaluation(
        "candidate",
        candidate_positive,
        [target, "skill:old"],
        [target, "skill:old"],
        routed=True,
    )

    comparison = helper._candidate_comparison(baseline, candidate, frozen)

    assert comparison["production_replay"]["accepted_order_changes"] == [case_id]
    assert comparison["replay"]["unaccepted_order_changes"] == [case_id]
    assert comparison["accepted"] is False


def test_snapshot_copy_keeps_only_scanner_inputs_and_name_only_skill_support(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live = tmp_path / "live"
    state = live / "knowledge"
    write(live / "docs" / "guide.md", "# Guide\n")
    write(live / "scripts" / "dirty-untracked.py", "print('scanner input')\n")
    write(live / "dirty-untracked.py", "print('outside configured script dirs')\n")
    write(live / "logs" / "private.md", "private operational log\n")
    write(live / "main_docker_server" / ".env.example", "PRIVATE=value\n")
    write(live / "private-zone" / "excluded.md", "configured exclusion\n")
    write(live / "custom_skills" / "alpha" / "SKILL.md", "---\nname: alpha\n---\n")
    write(live / "custom_skills" / "alpha" / "references" / "guide.md", "# Skill guide\n")
    write(live / "custom_skills" / "alpha" / "assets" / "private.bin", "private support payload\n")
    write(state / "README.md", "# Knowledge state\n")
    write(state / "index.jsonl", "private generated row\n")
    write(state / "okfs" / "tools" / "generated.md", "# generated\n")
    destination = tmp_path / "snapshot"

    helper.copy_scanner_snapshot(
        live,
        destination,
        state_root=state,
        settings={"exclude_dir_names": ["private-zone"], "include_markdown_docs": True},
    )

    assert (destination / "docs" / "guide.md").is_file()
    assert (destination / "scripts" / "dirty-untracked.py").is_file()
    assert not (destination / "dirty-untracked.py").exists()
    assert not (destination / "logs").exists()
    assert not (destination / "main_docker_server" / ".env.example").exists()
    assert not (destination / "private-zone").exists()
    assert (destination / "custom_skills" / "alpha" / "SKILL.md").is_file()
    assert (destination / "custom_skills" / "alpha" / "references" / "guide.md").read_text() == "# Skill guide\n"
    support_asset = destination / "custom_skills" / "alpha" / "assets" / "private.bin"
    assert support_asset.is_file()
    assert support_asset.read_bytes() == b""
    assert (destination / "knowledge" / "README.md").is_file()
    assert not (destination / "knowledge" / "index.jsonl").exists()
    assert not (destination / "knowledge" / "okfs").exists()


def test_snapshot_symlinks_are_rewritten_to_ref_clones(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_source = tmp_path / "live-source"
    live_hermes = tmp_path / "live-home" / ".hermes"
    write(live_source / "custom_skills" / "alpha" / "SKILL.md", "---\nname: alpha\n---\n")
    (live_hermes / "skills").mkdir(parents=True)
    (live_hermes / "skills" / "alpha").symlink_to(live_source / "custom_skills" / "alpha", target_is_directory=True)
    source_base = tmp_path / "base" / "source"
    runtime_base = tmp_path / "base" / "runtime"
    helper.copy_scanner_snapshot(live_source, source_base, state_root=live_source / "knowledge")
    helper.copy_runtime_snapshot(live_hermes, runtime_base)
    source_clone = tmp_path / "ref" / "source"
    runtime_clone = tmp_path / "ref" / "home" / ".hermes"
    helper.clone_snapshot(source_base, source_clone)
    helper.clone_snapshot(runtime_base, runtime_clone)
    (live_hermes / "skills" / "alpha").unlink()

    helper.rewrite_clone_symlinks(
        source_clone,
        runtime_clone,
        live_source=live_source,
        live_hermes_home=live_hermes,
        clone_source=source_clone,
        clone_hermes_home=runtime_clone,
    )
    helper.assert_isolated_topology(
        source_clone,
        runtime_clone,
        tmp_path / "ref" / "state",
        {"custom_skill_dirs": ["custom_skills"], "script_dirs": ["scripts"], "memory_dirs": ["memory"], "runbook_dirs": ["docs"]},
        live_roots=(live_source, live_hermes),
    )

    assert (runtime_clone / "skills" / "alpha").resolve() == (source_clone / "custom_skills" / "alpha").resolve()


def test_relative_snapshot_symlink_nested_under_live_root_is_not_double_remapped(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_source = tmp_path / "live-source"
    live_hermes = tmp_path / "live-home" / ".hermes"
    skill = live_source / "custom_skills" / "alpha"
    write(skill / "SKILL.md", "---\nname: alpha\n---\n")
    skills = live_hermes / "skills"
    skills.mkdir(parents=True)
    relative_target = Path(os.path.relpath(skill, skills))
    (skills / "alpha").symlink_to(relative_target, target_is_directory=True)
    source_base = tmp_path / "base" / "source"
    runtime_base = tmp_path / "base" / "runtime"
    helper.copy_scanner_snapshot(live_source, source_base, state_root=live_source / "knowledge")
    helper.copy_runtime_snapshot(live_hermes, runtime_base)
    clone_root = live_hermes / "cache" / "evaluation" / "ref"
    source_clone = clone_root / "source"
    runtime_clone = clone_root / "home" / ".hermes"
    state_clone = clone_root / "state"
    helper.clone_snapshot(source_base, source_clone)
    helper.clone_snapshot(runtime_base, runtime_clone)

    helper.rewrite_clone_symlinks(
        source_clone,
        runtime_clone,
        live_source=live_source,
        live_hermes_home=live_hermes,
        clone_source=source_clone,
        clone_hermes_home=runtime_clone,
    )
    helper.assert_isolated_topology(
        source_clone,
        runtime_clone,
        state_clone,
        {"custom_skill_dirs": ["custom_skills"], "script_dirs": ["scripts"], "memory_dirs": ["memory"], "runbook_dirs": ["docs"]},
        live_roots=(live_source, live_hermes),
    )

    rewritten = runtime_clone / "skills" / "alpha"
    assert rewritten.is_dir()
    assert rewritten.resolve() == (source_clone / "custom_skills" / "alpha").resolve()


def test_runtime_snapshot_fails_closed_for_uncopied_hermes_backed_skill_symlink(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_hermes = tmp_path / "live" / ".hermes"
    shared_skill = live_hermes / "shared-skills" / "alpha"
    write(
        shared_skill / "SKILL.md",
        "---\nname: alpha\ndescription: Hermes-backed shared skill.\n---\n",
    )
    (live_hermes / "skills").mkdir()
    (live_hermes / "skills" / "alpha").symlink_to(Path("../shared-skills/alpha"), target_is_directory=True)
    snapshot = tmp_path / "snapshot"

    with pytest.raises(RuntimeError, match="outside the copied Hermes skills subtree"):
        helper.copy_runtime_snapshot(live_hermes, snapshot)

    assert not snapshot.exists()


def test_runtime_snapshot_keeps_only_runtime_skill_scanner_inputs(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_hermes = tmp_path / "live" / ".hermes"
    skill = live_hermes / "skills" / "alpha"
    write(skill / "SKILL.md", "---\nname: alpha\n---\n")
    write(skill / "references" / "guide.md", "# Runtime guide\n")
    write(skill / "assets" / "private.bin", "private runtime payload\n")
    write(live_hermes / "skills" / ".usage.json", '{"private": true}\n')
    write(live_hermes / "skills" / "logs" / "private.md", "private log\n")
    snapshot = tmp_path / "snapshot"

    helper.copy_runtime_snapshot(live_hermes, snapshot)

    assert (snapshot / "skills" / "alpha" / "SKILL.md").is_file()
    assert (snapshot / "skills" / "alpha" / "references" / "guide.md").read_text() == "# Runtime guide\n"
    support_asset = snapshot / "skills" / "alpha" / "assets" / "private.bin"
    assert support_asset.is_file()
    assert support_asset.read_bytes() == b""
    assert not (snapshot / "skills" / ".usage.json").exists()
    assert not (snapshot / "skills" / "logs").exists()


def test_runtime_config_cron_and_okf_symlinks_are_materialized_without_touching_targets(tmp_path: Path) -> None:
    helper = load_compare_helper()
    external = tmp_path / "external"
    live_hermes = tmp_path / "live" / ".hermes"
    live_state = tmp_path / "live" / "state"
    external.mkdir()
    live_hermes.mkdir(parents=True)
    (live_hermes / "cron").mkdir()
    (live_state / "okfs" / "tools").mkdir(parents=True)
    config_target = external / "config.yaml"
    cron_target = external / "jobs.json"
    okf_target = external / "demo.md"
    write(config_target, f"local_knowledge:\n  source_root: {tmp_path / 'live-source'}\n")
    write(cron_target, '{"jobs": []}\n')
    write(okf_target, "---\nartifact_type: tool_okf\ntool: demo\nschema_hash: abc\n---\n")
    config_target.chmod(0o640)
    cron_target.chmod(0o644)
    okf_target.chmod(0o640)
    before = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (config_target, cron_target, okf_target)
    }
    (live_hermes / "config.yaml").symlink_to(config_target)
    (live_hermes / "cron" / "jobs.json").symlink_to(cron_target)
    (live_state / "okfs" / "tools" / "demo.md").symlink_to(okf_target)

    runtime_snapshot = tmp_path / "snapshot" / "runtime"
    okf_snapshot = tmp_path / "snapshot" / "okfs" / "tools"
    helper.copy_runtime_snapshot(live_hermes, runtime_snapshot)
    helper._copy_okfs(live_state / "okfs" / "tools", okf_snapshot)
    helper.rewrite_local_knowledge_config(
        runtime_snapshot / "config.yaml",
        ((tmp_path / "live-source", tmp_path / "clone-source"),),
    )

    for copied in (
        runtime_snapshot / "config.yaml",
        runtime_snapshot / "cron" / "jobs.json",
        okf_snapshot / "demo.md",
    ):
        assert copied.is_file()
        assert not copied.is_symlink()
    if os.name != "nt":
        assert stat.S_IMODE((runtime_snapshot / "config.yaml").stat().st_mode) == 0o600
        assert stat.S_IMODE((okf_snapshot / "demo.md").stat().st_mode) == 0o600
    assert {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (config_target, cron_target, okf_target)
    } == before


def test_isolation_allows_private_clones_nested_under_live_hermes_cache(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_source = tmp_path / "live-source"
    live_hermes = tmp_path / ".hermes"
    clone_root = live_hermes / "cache" / "evaluation" / "ref"
    clone_source = clone_root / "source"
    clone_hermes = clone_root / "home" / ".hermes"
    clone_state = clone_source / "knowledge"
    for path in (live_source, live_hermes / "skills", clone_source, clone_hermes / "skills", clone_state):
        path.mkdir(parents=True, exist_ok=True)

    helper.assert_isolated_topology(
        clone_source,
        clone_hermes,
        clone_state,
        {
            "custom_skill_dirs": ["custom_skills"],
            "script_dirs": ["scripts"],
            "memory_dirs": ["memory"],
            "runbook_dirs": ["docs"],
        },
        live_roots=(live_source, live_hermes),
    )

    escaped = clone_source / "custom_skills"
    escaped.symlink_to(live_source, target_is_directory=True)
    with pytest.raises(RuntimeError, match="configured scanner root still points"):
        helper.assert_isolated_topology(
            clone_source,
            clone_hermes,
            clone_state,
            {"custom_skill_dirs": ["custom_skills"]},
            live_roots=(live_source, live_hermes),
        )


def test_timing_summary_reports_builds_and_nearest_rank_p95() -> None:
    helper = load_compare_helper()
    output = {
        "_builds": {"full": {"duration_ms": 12.3456}, "synthetic": {"duration_ms": 2.0}},
        "label_search": {
            "a": {"duration_ms": 1.0},
            "b": {"duration_ms": 3.0},
        },
        "replay": {"search": {"c": {"duration_ms": 2.0}}},
        "synthetic": {"d": {"duration_ms": 4.0}},
    }

    assert helper.timing_summary(output) == {
        "full_build_ms": 12.346,
        "synthetic_build_ms": 2.0,
        "search_count": 4,
        "search_p95_ms": 4.0,
    }


def test_config_rewrite_changes_only_local_knowledge_path_fields(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_source = tmp_path / "live-source"
    clone_source = tmp_path / "clone-source"
    config = tmp_path / "config.yaml"
    write(
        config,
        f"""local_knowledge:
  source_root: {live_source}
  state_dir: {live_source / 'knowledge'}
  script_dirs:
    - scripts
    - {live_source / 'extra-scripts'}
  known_entities: [Alpha, Beta]
mcp:
  servers:
    demo:
      command: {live_source / 'scripts' / 'run.sh'}
""",
    )

    helper.rewrite_local_knowledge_config(
        config,
        ((live_source.resolve(), clone_source.resolve()),),
    )
    text = config.read_text(encoding="utf-8")

    assert f"source_root: {clone_source}" in text
    assert str(clone_source / "knowledge") in text
    assert str(clone_source / "extra-scripts") in text
    assert "known_entities: [Alpha, Beta]" in text
    assert f"command: {live_source / 'scripts' / 'run.sh'}" in text


def test_config_rewrite_does_not_rewrite_clone_paths_nested_under_live_hermes(tmp_path: Path) -> None:
    helper = load_compare_helper()
    live_source = tmp_path / "live-source"
    live_state = live_source / "knowledge"
    live_hermes = tmp_path / "live-home" / ".hermes"
    private_ref = live_hermes / "cache" / "evaluation" / "ref"
    clone_source = private_ref / "source"
    clone_state = clone_source / "knowledge"
    clone_hermes = private_ref / "home" / ".hermes"
    config = tmp_path / "config.yaml"
    write(
        config,
        f"""local_knowledge:
  source_root: {live_source}
  state_dir: {live_state}
  hermes_home: {live_hermes}
""",
    )

    helper.rewrite_local_knowledge_config(
        config,
        (
            (live_state, clone_state),
            (live_source, clone_source),
            (live_hermes, clone_hermes),
        ),
    )
    text = config.read_text(encoding="utf-8")

    assert f"source_root: {clone_source}\n" in text
    assert f"state_dir: {clone_state}\n" in text
    assert f"hermes_home: {clone_hermes}\n" in text
    assert text.count(str(private_ref)) == 3


def test_manifest_mutation_detection_is_fail_closed(tmp_path: Path) -> None:
    helper = load_compare_helper()
    root = tmp_path / "root"
    write(root / "docs" / "guide.md", "before\n")
    before = helper.tree_manifest(root)
    write(root / "docs" / "guide.md", "after\n")
    after = helper.tree_manifest(root)

    with pytest.raises(RuntimeError, match="changed"):
        helper.assert_manifest_unchanged("fixture", before, after)


def test_private_paths_and_files_are_owner_only(tmp_path: Path) -> None:
    helper = load_compare_helper()
    private = helper.ensure_private_directory(tmp_path / "private")
    output = private / "details.json"
    helper.write_private_json(output, {"query": "private"})

    if os.name != "nt":
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_compare_helper_cleanup_is_best_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    helper = load_compare_helper()
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
        calls.append(command)
        if command[:3] == ["git", "worktree", "remove"] and command[-1].endswith("bad"):
            raise OSError("remove failed")
        return ""

    monkeypatch.setattr(helper, "run", fake_run)
    created = [tmp_path / "good", tmp_path / "bad"]

    helper.cleanup_worktrees(created, keep=True)
    assert calls == []

    helper.cleanup_worktrees(created, keep=False)

    assert ["git", "worktree", "remove", "--force", str(tmp_path / "bad")] in calls
    assert ["git", "worktree", "remove", "--force", str(tmp_path / "good")] in calls
    assert ["git", "worktree", "prune"] in calls
    assert "cleanup failed" in capsys.readouterr().err


def test_cli_returns_nonzero_when_acceptance_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    helper = load_compare_helper()
    usage_db = tmp_path / "usage.sqlite"
    usage_db.touch()
    failed = helper.ComparisonRun(accepted=False, report={"accepted": False, "results": []}, details={})
    monkeypatch.setattr(helper, "compare_refs", lambda _args, _base_dir: failed)

    status = helper.main(["base", "candidate", "--usage-db", str(usage_db), "--work-dir", str(tmp_path / "eval"), "--json"])

    assert status == 1
    assert json.loads(capsys.readouterr().out)["accepted"] is False


def test_temporary_cleanup_failure_does_not_mask_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = load_compare_helper()
    usage_db = tmp_path / "usage.sqlite"
    usage_db.touch()
    temporary = tmp_path / "temporary-eval"
    monkeypatch.setattr(helper.tempfile, "mkdtemp", lambda **_kwargs: str(temporary))
    monkeypatch.setattr(
        helper,
        "compare_refs",
        lambda _args, _base_dir: (_ for _ in ()).throw(RuntimeError("primary failure")),
    )
    monkeypatch.setattr(helper.shutil, "rmtree", lambda _path: (_ for _ in ()).throw(OSError("cleanup failure")))

    status = helper.main(["base", "candidate", "--usage-db", str(usage_db), "--json"])
    captured = capsys.readouterr()

    assert status == 2
    assert json.loads(captured.out)["error_type"] == "RuntimeError"
    assert "private evaluation cleanup failed" in captured.err


def test_self_comparison_is_zero_diff_and_public_outputs_redact_private_canary(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    helper = load_compare_helper()
    canary = "privacy-canary-7f42c90e"
    artifact_id = f"skill:{canary}"
    query = f"{canary} query"
    source = tmp_path / f"source-{canary}"
    hermes_home = tmp_path / "live-home" / ".hermes"
    state = source / "knowledge"
    write(
        source / "custom_skills" / canary / "SKILL.md",
        f"""---
name: {canary}
description: {query} routing skill.
---
# Privacy canary
""",
    )
    write(
        source / "custom_skills" / "alpha" / "SKILL.md",
        "---\nname: alpha\ndescription: Alpha routing skill.\n---\n# Alpha\n",
    )
    write(source / "scripts" / "alpha.py", '"""Alpha helper."""\n')
    write(hermes_home / "config.yaml", f"local_knowledge:\n  source_root: {source}\n  state_dir: {state}\n")
    write(hermes_home / "cron" / "jobs.json", '{"jobs": []}\n')
    usage_db = tmp_path / "usage.sqlite"
    create_usage_db(usage_db, source)
    root_text = str(source.resolve())
    with sqlite3.connect(usage_db) as conn:
        conn.execute(
            """
            INSERT INTO usage_events (
                id, ts, tool, query, artifact_id, artifact_type, limit_value,
                rebuild_requested, success, result_count, root
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (20, "2026-01-01T00:02:00Z", "knowledge_search", query, None, None, 10, 0, 1, 1, root_text),
        )
        conn.execute(
            """
            INSERT INTO usage_events (
                id, ts, tool, query, artifact_id, artifact_type, limit_value,
                rebuild_requested, success, result_count, root
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (21, "2026-01-01T00:02:01Z", "knowledge_get", None, artifact_id, None, None, 0, 1, 1, root_text),
        )
        conn.execute(
            """
            INSERT INTO feedback (id, ts, event_id, rating, query, artifact_id, root)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (8, "2026-01-01T00:03:00Z", 20, "useful", None, artifact_id, root_text),
        )
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)

    def comparison_args(work_dir: Path, *extra: str) -> list[str]:
        return [
            "WORKTREE",
            "WORKTREE",
            "--usage-db",
            str(usage_db),
            "--hermes-home",
            str(hermes_home),
            "--root",
            str(source),
            "--work-dir",
            str(work_dir),
            *extra,
        ]

    json_work_dir = tmp_path / "default-json"
    status = helper.main(comparison_args(json_work_dir, "--json"))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert status == 0
    assert payload["accepted"] is True
    assert payload["comparisons"][0]["accepted"] is True
    assert payload["comparisons"][0]["structure"]["equal"] is True
    assert payload["results"][0]["synthetic"]["passed"] == 7
    assert payload["results"][1]["synthetic"]["passed"] == 7
    report_file = json_work_dir / "report.json"
    frozen_cases = json_work_dir / "frozen" / "cases.json"
    assert report_file.is_file()
    assert frozen_cases.is_file()
    assert canary not in captured.out
    assert canary not in captured.err
    assert canary not in report_file.read_text(encoding="utf-8")
    assert canary in frozen_cases.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(json_work_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(report_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(frozen_cases.stat().st_mode) == 0o600

    markdown_work_dir = tmp_path / "default-markdown"
    assert helper.main(comparison_args(markdown_work_dir)) == 0
    captured = capsys.readouterr()
    assert canary not in captured.out
    assert canary not in captured.err
    assert canary not in (markdown_work_dir / "report.json").read_text(encoding="utf-8")

    details_work_dir = tmp_path / "explicit-details"
    assert helper.main(comparison_args(details_work_dir, "--json", "--details")) == 0
    captured = capsys.readouterr()
    details_payload = json.loads(captured.out)
    details_file = details_work_dir / "details.json"
    assert canary in json.dumps(details_payload["details"])
    assert canary in details_file.read_text(encoding="utf-8")
    assert canary not in (details_work_dir / "report.json").read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(details_work_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(details_file.stat().st_mode) == 0o600

    def fail_with_private_value(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(canary)

    monkeypatch.setattr(helper, "compare_refs", fail_with_private_value)
    diagnostic_work_dir = tmp_path / "diagnostic"
    assert helper.main(comparison_args(diagnostic_work_dir, "--json")) == 2
    captured = capsys.readouterr()
    assert canary not in captured.out
    assert canary not in captured.err
    assert json.loads(captured.out)["error_type"] == "RuntimeError"
    assert not any(path.name == "usage.sqlite" for path in Path(__file__).resolve().parents[1].rglob("usage.sqlite"))
