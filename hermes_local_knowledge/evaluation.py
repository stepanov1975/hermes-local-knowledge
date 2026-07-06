"""Offline evaluation helpers for local knowledge search quality."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .search import search_index
from .storage import connect_readonly, decode_artifact_row

POSITIVE_FEEDBACK_RATINGS = frozenset({"useful", "great"})
IGNORED_LABEL_VALUES = frozenset({"", "none", "null", "xxxx", "sentinel unlikely", "demo"})


@dataclass(frozen=True)
class SearchMetrics:
    """Top-k replay metrics for historical query labels."""

    query_count: int
    label_count: int
    hit_at_1: float
    hit_at_3: float
    hit_at_5: float
    hit_at_10: float
    mrr_at_10: float
    parent_equiv_hit_at_1: float
    parent_equiv_hit_at_3: float
    parent_equiv_hit_at_5: float
    parent_equiv_hit_at_10: float
    parent_equiv_mrr_at_10: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "query_count": self.query_count,
            "label_count": self.label_count,
            "hit_at_1": self.hit_at_1,
            "hit_at_3": self.hit_at_3,
            "hit_at_5": self.hit_at_5,
            "hit_at_10": self.hit_at_10,
            "mrr_at_10": self.mrr_at_10,
            "parent_equiv_hit_at_1": self.parent_equiv_hit_at_1,
            "parent_equiv_hit_at_3": self.parent_equiv_hit_at_3,
            "parent_equiv_hit_at_5": self.parent_equiv_hit_at_5,
            "parent_equiv_hit_at_10": self.parent_equiv_hit_at_10,
            "parent_equiv_mrr_at_10": self.parent_equiv_mrr_at_10,
        }


@dataclass(frozen=True)
class SearchLabelResult:
    """Per-query replay result for historical search labels."""

    query: str
    expected_ids: tuple[str, ...]
    exact_rank: int | None
    parent_equiv_rank: int | None
    top_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "expected_ids": list(self.expected_ids),
            "exact_rank": self.exact_rank,
            "parent_equiv_rank": self.parent_equiv_rank,
            "top_ids": list(self.top_ids),
        }


@dataclass(frozen=True)
class SearchEvaluationReport:
    """Aggregate metrics plus per-query replay details."""

    metrics: SearchMetrics
    cases: tuple[SearchLabelResult, ...]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = self.metrics.as_dict()
        payload["cases"] = [case.as_dict() for case in self.cases]
        return payload


def _clean_label_value(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in IGNORED_LABEL_VALUES else text


def artifact_ids(db_path: Path) -> set[str]:
    conn = connect_readonly(db_path)
    try:
        return {str(row[0]) for row in conn.execute("SELECT id FROM artifacts").fetchall()}
    finally:
        conn.close()


def artifact_parent_equivalence_map(db_path: Path) -> dict[str, set[str]]:
    """Return only parent/child equivalences that evaluation should relax.

    The parent-equivalent metric is intentionally narrower than graph-neighbor
    matching. Today it only treats skill support documents and their owning
    skill as equivalent, because historical labels often point at the parent
    skill while newer search correctly surfaces a more-specific support doc.
    Peer skills, cron-script links, keyword-overlap edges, and other graph
    relationships are useful context but are not evaluation equivalence.
    """

    conn = connect_readonly(db_path)
    try:
        rows = conn.execute("SELECT * FROM artifacts").fetchall()
    finally:
        conn.close()
    equivalents: dict[str, set[str]] = {}
    for row in rows:
        artifact = decode_artifact_row(row)
        artifact_id = str(artifact["id"])
        if artifact.get("type") != "skill_support_doc":
            continue
        for related in artifact.get("related") or []:
            related_id = str(related)
            if not related_id.startswith("skill:"):
                continue
            equivalents.setdefault(artifact_id, set()).add(related_id)
            equivalents.setdefault(related_id, set()).add(artifact_id)
    return equivalents


def load_positive_feedback_labels(
    usage_db_path: Path,
    *,
    valid_artifact_ids: set[str] | None = None,
) -> dict[str, set[str]]:
    """Load deduplicated positive query labels from local feedback telemetry.

    Positive feedback is useful as an evaluation oracle, not as training truth:
    labels can age as support docs or more-specific artifacts are added. The
    caller can pass ``valid_artifact_ids`` to discard stale artifact labels.
    """

    conn = sqlite3.connect(f"file:{usage_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT f.rating, COALESCE(f.query, e.query) AS query, f.artifact_id
            FROM feedback f
            LEFT JOIN usage_events e ON e.id = f.event_id
            WHERE f.rating IN ('useful', 'great')
              AND COALESCE(f.query, e.query) IS NOT NULL
              AND f.artifact_id IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()

    labels: dict[str, set[str]] = {}
    for row in rows:
        if str(row["rating"]).strip().lower() not in POSITIVE_FEEDBACK_RATINGS:
            continue
        query = _clean_label_value(row["query"])
        artifact_id = _clean_label_value(row["artifact_id"])
        if not query or not artifact_id:
            continue
        if valid_artifact_ids is not None and artifact_id not in valid_artifact_ids:
            continue
        labels.setdefault(query, set()).add(artifact_id)
    return labels


def _matches_with_parent_equivalence(
    result_id: str,
    expected_ids: set[str],
    parent_equivalents: Mapping[str, set[str]],
) -> bool:
    if result_id in expected_ids:
        return True
    return bool(parent_equivalents.get(result_id, set()) & expected_ids)


def evaluate_search_labels(
    labels: Mapping[str, set[str]],
    search_fn: Callable[[str, int], Sequence[str]],
    *,
    parent_equivalents: Mapping[str, set[str]] | None = None,
    max_k: int = 10,
) -> SearchMetrics:
    """Replay labeled queries and compute exact and parent-equivalent metrics."""

    return evaluate_search_labels_report(
        labels,
        search_fn,
        parent_equivalents=parent_equivalents,
        max_k=max_k,
    ).metrics


def evaluate_search_labels_report(
    labels: Mapping[str, set[str]],
    search_fn: Callable[[str, int], Sequence[str]],
    *,
    parent_equivalents: Mapping[str, set[str]] | None = None,
    max_k: int = 10,
) -> SearchEvaluationReport:
    """Replay labeled queries and include per-query ranks/top results."""

    parent_equivalents = parent_equivalents or {}
    metric_limit = 10
    search_limit = max(metric_limit, int(max_k))
    counters = {1: 0, 3: 0, 5: 0, 10: 0}
    parent_counters = {1: 0, 3: 0, 5: 0, 10: 0}
    reciprocal_rank = 0.0
    parent_reciprocal_rank = 0.0
    query_count = 0
    label_count = 0
    cases: list[SearchLabelResult] = []

    for query, expected_ids in labels.items():
        expected = {item for item in expected_ids if item}
        if not query or not expected:
            continue
        query_count += 1
        label_count += len(expected)
        result_ids = [str(item) for item in search_fn(query, search_limit)]
        exact_rank: int | None = None
        parent_rank: int | None = None
        for rank, result_id in enumerate(result_ids[:metric_limit], start=1):
            if exact_rank is None and result_id in expected:
                exact_rank = rank
            if parent_rank is None and _matches_with_parent_equivalence(result_id, expected, parent_equivalents):
                parent_rank = rank
        for k in counters:
            if exact_rank is not None and exact_rank <= k:
                counters[k] += 1
            if parent_rank is not None and parent_rank <= k:
                parent_counters[k] += 1
        if exact_rank is not None:
            reciprocal_rank += 1 / exact_rank
        if parent_rank is not None:
            parent_reciprocal_rank += 1 / parent_rank
        cases.append(
            SearchLabelResult(
                query=query,
                expected_ids=tuple(sorted(expected)),
                exact_rank=exact_rank,
                parent_equiv_rank=parent_rank,
                top_ids=tuple(result_ids[:metric_limit]),
            )
        )

    denominator = query_count or 1
    metrics = SearchMetrics(
        query_count=query_count,
        label_count=label_count,
        hit_at_1=counters[1] / denominator,
        hit_at_3=counters[3] / denominator,
        hit_at_5=counters[5] / denominator,
        hit_at_10=counters[10] / denominator,
        mrr_at_10=reciprocal_rank / denominator,
        parent_equiv_hit_at_1=parent_counters[1] / denominator,
        parent_equiv_hit_at_3=parent_counters[3] / denominator,
        parent_equiv_hit_at_5=parent_counters[5] / denominator,
        parent_equiv_hit_at_10=parent_counters[10] / denominator,
        parent_equiv_mrr_at_10=parent_reciprocal_rank / denominator,
    )
    return SearchEvaluationReport(metrics=metrics, cases=tuple(cases))


def evaluate_index_against_feedback(db_path: Path, usage_db_path: Path) -> SearchMetrics:
    return evaluate_index_against_feedback_report(db_path, usage_db_path).metrics


def evaluate_index_against_feedback_report(db_path: Path, usage_db_path: Path) -> SearchEvaluationReport:
    valid_ids = artifact_ids(db_path)
    labels = load_positive_feedback_labels(usage_db_path, valid_artifact_ids=valid_ids)
    parent_equivalents = artifact_parent_equivalence_map(db_path)

    def search_ids(query: str, limit: int) -> list[str]:
        return [str(row["id"]) for row in search_index(db_path, query, limit=limit)]

    return evaluate_search_labels_report(labels, search_ids, parent_equivalents=parent_equivalents)
