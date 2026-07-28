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
    assert len(frozen.replay_search) == 3  # rebuild variants coalesce into the same public input tuple
    assert next(case for case in frozen.replay_search if case.query == "alpha").rebuild_requested is True
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
