from __future__ import annotations

import pytest

from hermes_local_knowledge.evaluation import (
    evaluate_search_labels,
    evaluate_search_labels_report,
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
