from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from hermes_local_knowledge.evaluation import (
    evaluate_search_labels,
    evaluate_search_labels_report,
    load_positive_feedback_labels,
    load_quality_tiered_feedback_labels,
)


def test_at_ten_metrics_ignore_rank_eleven_even_when_max_k_is_larger() -> None:
    captured_limits: list[int] = []

    def rank_eleven(_query: str, limit: int) -> list[str]:
        captured_limits.append(limit)
        return [*(f"noise-{index}" for index in range(10)), "target"]

    metrics = evaluate_search_labels({"query": {"target"}}, rank_eleven, max_k=11)

    assert captured_limits == [11]
    assert metrics.hit_at_10 == 0.0
    assert metrics.mrr_at_10 == 0.0


def test_max_k_below_ten_still_uses_the_actual_top_ten_window() -> None:
    captured_limits: list[int] = []

    def rank_ten(_query: str, limit: int) -> list[str]:
        captured_limits.append(limit)
        return [*(f"noise-{index}" for index in range(9)), "target", "rank-eleven"]

    report = evaluate_search_labels_report({"query": {"target"}}, rank_ten, max_k=5)

    assert captured_limits == [10]
    assert report.metrics.hit_at_5 == 0.0
    assert report.metrics.hit_at_10 == 1.0
    assert report.metrics.mrr_at_10 == pytest.approx(0.1)
    assert report.cases[0].exact_rank == 10
    assert report.cases[0].top_ids == tuple([*(f"noise-{index}" for index in range(9)), "target"])


def _create_quality_usage_db(path: Path, live_root: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY,
                tool TEXT,
                success INTEGER,
                root TEXT,
                query TEXT,
                top_ids_json TEXT
            );
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY,
                event_id INTEGER,
                rating TEXT,
                query TEXT,
                artifact_id TEXT,
                root TEXT,
                expected_artifact_id TEXT,
                resolves_feedback_id INTEGER,
                linkage_status TEXT
            );
            """
        )
        root = str(live_root)
        other = str(live_root.parent / "other")
        conn.executemany(
            "INSERT INTO usage_events VALUES (?, 'knowledge_search', 1, ?, ?, ?)",
            [
                (1, root, "verification query", '["skill:accepted"]'),
                (2, root, "trigger query", '["skill:rejected"]'),
                (3, root, "verified query", '["skill:verified"]'),
                (4, other, "wrong root", '["skill:accepted"]'),
                (5, root, "malformed event query", '["skill:other"]'),
            ],
        )
        conn.executemany(
            """
            INSERT INTO feedback (
                id, event_id, rating, query, artifact_id, root,
                resolves_feedback_id, linkage_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (10, 2, "not_useful", "trigger query", "skill:rejected", root, None, "verified_event"),
                (11, 1, "useful", "verification query", "skill:accepted", root, 10, "verified_event"),
                (12, None, "useful", "direct query", "skill:direct", root, None, "direct_query"),
                (13, 1, "useful", "verification query", "skill:accepted", root, 999, "verified_event"),
                (14, 4, "useful", "wrong root", "skill:accepted", other, None, "verified_event"),
                (15, 5, "useful", "malformed query", "skill:accepted", root, None, "verified_event"),
                (16, 3, "useful", "verified query", "skill:verified", root, None, "verified_event"),
            ],
        )
        conn.execute(
            "UPDATE feedback SET expected_artifact_id = 'skill:accepted' WHERE id = 10"
        )


def test_quality_tiers_keep_legacy_aggregate_and_require_verified_scoped_links(tmp_path: Path) -> None:
    usage_db = tmp_path / "usage.sqlite"
    live_root = tmp_path / "live"
    _create_quality_usage_db(usage_db, live_root)
    valid_ids = {"skill:accepted", "skill:direct", "skill:rejected", "skill:verified"}

    corpus = load_quality_tiered_feedback_labels(
        usage_db,
        valid_artifact_ids=valid_ids,
        root=live_root,
    )

    assert corpus.labels == {
        "direct query": {"skill:direct"},
        "malformed query": {"skill:accepted"},
        "verification query": {"skill:accepted"},
        "verified query": {"skill:verified"},
    }
    assert corpus.labels_by_tier == {
        "explicit_resolution": {"trigger query": {"skill:accepted"}},
        "verified_event": {"verified query": {"skill:verified"}},
        "direct_or_legacy": {"direct query": {"skill:direct"}},
    }
    assert {item.quality_tier for item in corpus.provenance_by_query["trigger query"]} == {
        "explicit_resolution"
    }
    assert "wrong root" not in corpus.labels


def test_explicit_resolution_allows_parent_without_predeclared_target(tmp_path: Path) -> None:
    usage_db = tmp_path / "usage.sqlite"
    live_root = tmp_path / "live"
    _create_quality_usage_db(usage_db, live_root)
    with sqlite3.connect(usage_db) as conn:
        conn.execute("UPDATE feedback SET expected_artifact_id = NULL WHERE id = 10")

    corpus = load_quality_tiered_feedback_labels(
        usage_db,
        valid_artifact_ids={"skill:accepted"},
        root=live_root,
    )

    assert corpus.labels_by_tier["explicit_resolution"] == {
        "trigger query": {"skill:accepted"}
    }


def test_explicit_resolution_requires_parent_expected_target_match(tmp_path: Path) -> None:
    usage_db = tmp_path / "usage.sqlite"
    live_root = tmp_path / "live"
    _create_quality_usage_db(usage_db, live_root)
    with sqlite3.connect(usage_db) as conn:
        conn.execute(
            "UPDATE feedback SET expected_artifact_id = 'skill:different' WHERE id = 10"
        )

    corpus = load_quality_tiered_feedback_labels(
        usage_db,
        valid_artifact_ids={"skill:accepted", "skill:direct", "skill:rejected", "skill:verified"},
        root=live_root,
    )

    assert "trigger query" not in corpus.labels_by_tier["explicit_resolution"]


def test_quality_loader_preserves_legacy_empty_query_exclusion(tmp_path: Path) -> None:
    usage_db = tmp_path / "usage.sqlite"
    live_root = tmp_path / "live"
    _create_quality_usage_db(usage_db, live_root)
    with sqlite3.connect(usage_db) as conn:
        conn.execute("UPDATE feedback SET query = '' WHERE id = 16")

    corpus = load_quality_tiered_feedback_labels(
        usage_db,
        valid_artifact_ids={"skill:verified"},
        root=live_root,
    )

    assert corpus.labels == {}
    assert corpus.labels_by_tier["verified_event"] == {}


def test_explicit_resolution_requires_verified_parent_link(tmp_path: Path) -> None:
    usage_db = tmp_path / "usage.sqlite"
    live_root = tmp_path / "live"
    _create_quality_usage_db(usage_db, live_root)
    with sqlite3.connect(usage_db) as conn:
        conn.execute("UPDATE feedback SET linkage_status = 'legacy' WHERE id = 10")

    corpus = load_quality_tiered_feedback_labels(
        usage_db,
        valid_artifact_ids={"skill:accepted"},
        root=live_root,
    )

    assert corpus.labels_by_tier["explicit_resolution"] == {}


def test_explicit_resolution_requires_unique_resolving_row(tmp_path: Path) -> None:
    usage_db = tmp_path / "usage.sqlite"
    live_root = tmp_path / "live"
    _create_quality_usage_db(usage_db, live_root)
    with sqlite3.connect(usage_db) as conn:
        conn.execute(
            """
            INSERT INTO feedback (
                id, event_id, rating, query, artifact_id, root,
                expected_artifact_id, resolves_feedback_id, linkage_status
            ) VALUES (17, 1, 'useful', 'verification query', 'skill:accepted', ?, NULL, 10, 'verified_event')
            """,
            (str(live_root),),
        )

    corpus = load_quality_tiered_feedback_labels(
        usage_db,
        valid_artifact_ids={"skill:accepted"},
        root=live_root,
    )

    assert corpus.labels_by_tier["explicit_resolution"] == {}


def test_great_rating_is_aggregate_only_not_verified_resolution(tmp_path: Path) -> None:
    usage_db = tmp_path / "usage.sqlite"
    live_root = tmp_path / "live"
    _create_quality_usage_db(usage_db, live_root)
    with sqlite3.connect(usage_db) as conn:
        conn.execute("UPDATE feedback SET rating = 'great' WHERE id = 11")
    corpus = load_quality_tiered_feedback_labels(usage_db, root=live_root)
    assert corpus.labels["verification query"] == {"skill:accepted"}
    assert "trigger query" not in corpus.labels_by_tier["explicit_resolution"]


def test_legacy_label_loader_defaults_when_quality_columns_are_absent(tmp_path: Path) -> None:
    usage_db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(usage_db) as conn:
        conn.executescript(
            """
            CREATE TABLE usage_events (id INTEGER PRIMARY KEY, query TEXT);
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY,
                event_id INTEGER,
                rating TEXT,
                query TEXT,
                artifact_id TEXT
            );
            INSERT INTO feedback VALUES (1, NULL, 'useful', 'legacy query', 'skill:legacy');
            """
        )

    assert load_positive_feedback_labels(usage_db) == {"legacy query": {"skill:legacy"}}
    corpus = load_quality_tiered_feedback_labels(usage_db)
    assert corpus.labels_by_tier == {
        "explicit_resolution": {},
        "verified_event": {},
        "direct_or_legacy": {},
    }
