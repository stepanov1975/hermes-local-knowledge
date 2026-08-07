"""Offline evaluation helpers for local knowledge search quality."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
from typing import Any

from .index import connect_readonly, decode_artifact_row, index_source_root, search_index

POSITIVE_FEEDBACK_RATINGS = frozenset({"useful", "great"})
HIGH_CONFIDENCE_POSITIVE_RATINGS = frozenset({"useful"})
NEGATIVE_FEEDBACK_RATINGS = frozenset({"missing", "noisy", "not_useful", "stale", "wrong_artifact"})
IGNORED_LABEL_VALUES = frozenset({"", "none", "null", "xxxx", "sentinel unlikely", "demo"})
LABEL_QUALITY_TIERS = ("explicit_resolution", "verified_event", "direct_or_legacy")


@dataclass(frozen=True)
class FeedbackLabelProvenance:
    """Local evidence describing why one artifact is an accepted query label."""

    quality_tier: str
    feedback_id: int
    artifact_id: str
    linkage_status: str
    resolves_feedback_id: int | None = None
    trigger_query: str | None = None
    verification_query: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "quality_tier": self.quality_tier,
            "feedback_id": self.feedback_id,
            "artifact_id": self.artifact_id,
            "linkage_status": self.linkage_status,
        }
        if self.resolves_feedback_id is not None:
            payload["resolves_feedback_id"] = self.resolves_feedback_id
        if self.trigger_query is not None:
            payload["trigger_query"] = self.trigger_query
        if self.verification_query is not None:
            payload["verification_query"] = self.verification_query
        return payload


@dataclass(frozen=True)
class FeedbackLabelCorpus:
    """Aggregate-compatible labels plus disjoint evidence-quality tiers."""

    labels: dict[str, set[str]]
    labels_by_tier: dict[str, dict[str, set[str]]]
    provenance_by_query: dict[str, tuple[FeedbackLabelProvenance, ...]]


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
    label_provenance: tuple[FeedbackLabelProvenance, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": self.query,
            "expected_ids": list(self.expected_ids),
            "exact_rank": self.exact_rank,
            "parent_equiv_rank": self.parent_equiv_rank,
            "top_ids": list(self.top_ids),
        }
        return payload


@dataclass(frozen=True)
class SearchEvaluationReport:
    """Aggregate metrics plus per-query replay details."""

    metrics: SearchMetrics
    cases: tuple[SearchLabelResult, ...]
    quality_tier_metrics: Mapping[str, SearchMetrics] = field(default_factory=dict)

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

    return load_quality_tiered_feedback_labels(
        usage_db_path,
        valid_artifact_ids=valid_artifact_ids,
    ).labels


def _table_columns(conn: Any, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _column(alias: str, columns: set[str], name: str, default: str = "NULL") -> str:
    return f'{alias}."{name}"' if name in columns else default


def _valid_event_link(row: Any, *, expected_root: str) -> bool:
    try:
        top_ids = json.loads(str(row["event_top_ids_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        str(row["linkage_status"] or "") == "verified_event"
        and row["event_id"] is not None
        and str(row["event_tool"] or "") == "knowledge_search"
        and int(row["event_success"] or 0) == 1
        and str(row["feedback_root"] or "") == expected_root
        and str(row["event_root"] or "") == expected_root
        and _clean_label_value(row["feedback_query"]) == _clean_label_value(row["event_query"])
        and isinstance(top_ids, list)
        and _clean_label_value(row["artifact_id"]) in {str(value) for value in top_ids}
    )


def load_quality_tiered_feedback_labels(
    usage_db_path: Path,
    *,
    valid_artifact_ids: set[str] | None = None,
    root: Path | str | None = None,
) -> FeedbackLabelCorpus:
    """Load valid positive labels and classify their replay evidence.

    The returned ``labels`` mapping intentionally preserves the legacy aggregate
    contract.  New callers can inspect disjoint quality tiers and row-level
    provenance without excluding useful historical query labels.
    """

    resolution_counts: dict[int, int] = {}
    conn = connect_readonly(usage_db_path)
    try:
        feedback_columns = _table_columns(conn, "feedback")
        event_columns = _table_columns(conn, "usage_events")
        if not feedback_columns or not event_columns:
            return FeedbackLabelCorpus({}, {tier: {} for tier in LABEL_QUALITY_TIERS}, {})
        def f(name: str, default: str = "NULL") -> str:
            return _column("f", feedback_columns, name, default)

        def e(name: str, default: str = "NULL") -> str:
            return _column("e", event_columns, name, default)

        rows = conn.execute(
            f"""
            SELECT f.id AS feedback_id,
                   {f('rating', "''")} AS rating,
                   {f('query')} AS feedback_query,
                   COALESCE({f('query')}, {e('query')}) AS effective_query,
                   {f('artifact_id')} AS artifact_id,
                   {f('root')} AS feedback_root,
                   {f('event_id')} AS event_id,
                   {f('linkage_status', "'legacy'")} AS linkage_status,
                   {f('resolves_feedback_id')} AS resolves_feedback_id,
                   {e('tool')} AS event_tool,
                   {e('success', '0')} AS event_success,
                   {e('root')} AS event_root,
                   {e('query')} AS event_query,
                   {e('top_ids_json', "'[]'")} AS event_top_ids_json
            FROM feedback f
            LEFT JOIN usage_events e ON e.id = {f('event_id')}
            WHERE {f('rating', "''")} IN ('useful', 'great')
            """
        ).fetchall()

        feedback_by_id: dict[int, Any] = {}
        if "resolves_feedback_id" in feedback_columns:
            parent_rows = conn.execute(
                f"""
                SELECT f.id AS feedback_id,
                       {f('rating', "''")} AS rating,
                       {f('query')} AS feedback_query,
                       COALESCE({f('query')}, {e('query')}) AS effective_query,
                       {f('artifact_id')} AS artifact_id,
                       {f('expected_artifact_id')} AS expected_artifact_id,
                       {f('root')} AS feedback_root,
                       {f('event_id')} AS event_id,
                       {f('linkage_status', "'legacy'")} AS linkage_status,
                       NULL AS resolves_feedback_id,
                       {e('tool')} AS event_tool,
                       {e('success', '0')} AS event_success,
                       {e('root')} AS event_root,
                       {e('query')} AS event_query,
                       {e('top_ids_json', "'[]'")} AS event_top_ids_json
                FROM feedback f
                LEFT JOIN usage_events e ON e.id = {f('event_id')}
                WHERE f.id IN (
                    SELECT resolves_feedback_id FROM feedback WHERE resolves_feedback_id IS NOT NULL
                )
                """
            ).fetchall()
            feedback_by_id = {int(row["feedback_id"]): row for row in parent_rows}
            resolution_counts = {
                int(parent_id): int(count)
                for parent_id, count in conn.execute(
                    """
                    SELECT resolves_feedback_id, COUNT(*)
                    FROM feedback
                    WHERE resolves_feedback_id IS NOT NULL
                    GROUP BY resolves_feedback_id
                    """
                ).fetchall()
            }
    finally:
        conn.close()

    labels: dict[str, set[str]] = {}
    labels_by_tier: dict[str, dict[str, set[str]]] = {tier: {} for tier in LABEL_QUALITY_TIERS}
    provenance: dict[str, list[FeedbackLabelProvenance]] = {}
    requested_root = str(Path(root).expanduser().resolve()) if root is not None else None
    for row in rows:
        rating_text = str(row["rating"]).strip().lower()
        if rating_text not in POSITIVE_FEEDBACK_RATINGS:
            continue
        row_root_value = row["feedback_root"] if row["feedback_root"] is not None else row["event_root"]
        row_root = str(row_root_value or "")
        if requested_root is not None and row_root != requested_root:
            continue
        linkage_status = str(row["linkage_status"] or "legacy")
        aggregate_query = _clean_label_value(row["effective_query"])
        artifact_id = _clean_label_value(row["artifact_id"])
        if not aggregate_query or not artifact_id:
            continue
        if valid_artifact_ids is not None and artifact_id not in valid_artifact_ids:
            continue
        labels.setdefault(aggregate_query, set()).add(artifact_id)
        if rating_text not in HIGH_CONFIDENCE_POSITIVE_RATINGS:
            continue
        tier: str | None = "direct_or_legacy"
        tier_query = aggregate_query
        resolves_feedback_id = row["resolves_feedback_id"]
        trigger_query: str | None = None
        verification_query: str | None = None
        if resolves_feedback_id is not None:
            parent = feedback_by_id.get(int(resolves_feedback_id))
            if parent is None or resolution_counts.get(int(resolves_feedback_id)) != 1:
                tier = None
            else:
                parent_root_value = (
                    parent["feedback_root"]
                    if parent["feedback_root"] is not None
                    else parent["event_root"]
                )
                parent_root = str(parent_root_value or "")
                parent_linkage = str(parent["linkage_status"] or "legacy")
                parent_valid = (
                    bool(row_root)
                    and parent_root == row_root
                    and str(parent["rating"] or "").strip().lower() in NEGATIVE_FEEDBACK_RATINGS
                    and _clean_label_value(parent["expected_artifact_id"]) in {"", artifact_id}
                    and _valid_event_link(row, expected_root=row_root)
                    and parent_linkage == "verified_event"
                    and _valid_event_link(parent, expected_root=row_root)
                )
                trigger_query = _clean_label_value(parent["event_query"])
                if not parent_valid or not trigger_query:
                    tier = None
                else:
                    verification_query = _clean_label_value(row["event_query"])
                    tier_query = trigger_query
                    tier = "explicit_resolution"
        elif linkage_status == "verified_event":
            if not row_root or not _valid_event_link(row, expected_root=row_root):
                tier = None
            else:
                tier = "verified_event"
        elif not row_root or linkage_status not in {"direct_query", "legacy"}:
            tier = None
        if tier is not None:
            labels_by_tier[tier].setdefault(tier_query, set()).add(artifact_id)
        provenance_query = tier_query if tier is not None else aggregate_query
        provenance.setdefault(provenance_query, []).append(
            FeedbackLabelProvenance(
                quality_tier=tier or "aggregate_only",
                feedback_id=int(row["feedback_id"]),
                artifact_id=artifact_id,
                linkage_status=linkage_status,
                resolves_feedback_id=(None if resolves_feedback_id is None else int(resolves_feedback_id)),
                trigger_query=trigger_query,
                verification_query=verification_query,
            )
        )
    return FeedbackLabelCorpus(
        labels,
        labels_by_tier,
        {
            query: tuple(sorted(items, key=lambda item: (item.quality_tier, item.feedback_id, item.artifact_id)))
            for query, items in provenance.items()
        },
    )


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
    corpus = load_quality_tiered_feedback_labels(
        usage_db_path,
        valid_artifact_ids=valid_ids,
        root=index_source_root(db_path),
    )
    parent_equivalents = artifact_parent_equivalence_map(db_path)

    cached_results: dict[tuple[str, int], list[str]] = {}

    def search_ids(query: str, limit: int) -> list[str]:
        key = (query, limit)
        if key not in cached_results:
            cached_results[key] = [str(row["id"]) for row in search_index(db_path, query, limit=limit)]
        return cached_results[key]

    report = evaluate_search_labels_report(corpus.labels, search_ids, parent_equivalents=parent_equivalents)
    tier_metrics = {
        tier: evaluate_search_labels(
            corpus.labels_by_tier[tier],
            search_ids,
            parent_equivalents=parent_equivalents,
        )
        for tier in LABEL_QUALITY_TIERS
    }
    cases = tuple(
        replace(case, label_provenance=corpus.provenance_by_query.get(case.query, ()))
        for case in report.cases
    )
    return SearchEvaluationReport(report.metrics, cases, tier_metrics)
