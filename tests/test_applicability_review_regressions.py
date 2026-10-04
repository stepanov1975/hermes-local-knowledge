"""Deterministic regressions for cached-applicability review findings."""
from __future__ import annotations

import copy
import json
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from hermes_local_knowledge import applicability, index, shadow, shadow_sources, telemetry
from hermes_local_knowledge.config import Config, IndexSettings, VerifiedRoutingSettings
from hermes_local_knowledge.routing import RouteDecision, RouteOutcome
from hermes_local_knowledge.shadow_sources import Evidence, identity


TARGET = "runbook:docs-atlas"


def config(tmp_path: Path) -> Config:
    root, home = tmp_path / "corpus", tmp_path / "profile"
    (root / "docs").mkdir(parents=True)
    home.mkdir()
    (root / "docs" / "atlas.md").write_text(
        "# Atlas restore runbook\nOnly the secondary Atlas server is supported.\nDo not use on the primary server.\n")
    cfg = Config(root, home, tmp_path / "state", IndexSettings(),
                 verified_routing=VerifiedRoutingSettings(mode="veto"))
    index.build_index(root, cfg.state_dir, home, cfg.index_settings)
    return cfg


def result_packet(cfg: Config, *, ranges: str = "full") -> tuple[dict[str, Any], dict[str, Any]]:
    rows = index.search_index(cfg.state_dir / "index.sqlite", "Atlas restore", limit=2)
    decision = RouteDecision(rows=rows, outcome=RouteOutcome.PROMOTED_EXISTING,
                             feedback_id=1, artifact_id=TARGET, feedback_max_id=1)
    promotion = applicability.binding(cfg, request="Restore the primary Atlas server", query="Atlas restore",
                                      artifact_type="", limit=2, lookup=None, baseline=rows, decision=decision)
    evidence = Evidence(cfg, "")
    evidence.include([TARGET])
    if ranges == "full":
        source = evidence.read(TARGET)
    else:
        source = evidence.read(TARGET, start_line=1, end_line=1)
        if ranges == "multiple":
            source = evidence.read(TARGET, start_line=3, end_line=3)
    citation = {**{key: source[key] for key in ("id", "locator", "sha256")},
                "start_line": 1, "end_line": 1}
    result = {"provenance": "ai_promotion_applicability", "promotion": promotion,
              "contract_version": shadow.VERIFICATION_CONTRACT, "verdict": "inapplicable",
              "basis": "scope_target_incompatibility", "sources": [identity(source)],
              "citations": [citation]}
    return result, promotion


@pytest.mark.parametrize("promotion", [{}, None])
def test_promotion_receipts_do_not_crowd_out_shadow_reuse_scan(tmp_path: Path, promotion: Any) -> None:
    cfg = config(tmp_path)
    shadow.observe(cfg, user_request="Find Atlas restore instructions", query="Atlas restore",
                   artifact_type="", session_id="session", task_id="task", turn_id="turn",
                   baseline_ids=[TARGET])
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        original = dict(conn.execute("SELECT * FROM cases").fetchone())
        verified_at = time.time() - 200
        conn.execute("UPDATE cases SET status='ai_verified',verified_at=?,result=? WHERE id=?",
                     (verified_at, json.dumps({"contract_version": shadow.VERIFICATION_CONTRACT,
                                               "route_ids": [TARGET]}), original["id"]))
        columns = list(original)
        for n in range(shadow.MAX_REUSE_SCAN):
            row = {**original, "id": f"promotion-{n}", "status": "ai_verified",
                   "verified_at": verified_at + n + 1,
                   "lookup_context": json.dumps({**json.loads(original["lookup_context"]), "promotion": promotion})}
            conn.execute(f"INSERT INTO cases ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         [row[key] for key in columns])
    current = {**original, "id": "new-request"}
    candidates = shadow._reuse_candidates(cfg, current, shadow._task_packet(current))
    assert [row["id"] for row in candidates] == [original["id"]]


def test_usage_report_excludes_veto_from_changes_but_preserves_outcome(tmp_path: Path) -> None:
    root, db = tmp_path / "corpus", tmp_path / "usage.sqlite"
    for outcome in ("none", "promoted_existing", "promoted_retry", "applicability_vetoed"):
        assert telemetry._record_usage(root, tool="knowledge_search", success=True, query="Atlas restore",
                                       top_ids=[TARGET], baseline_top_ids=[TARGET], result_count=1,
                                       route_outcome=outcome, route_feedback_id=7, route_artifact_id=TARGET,
                                       usage_db_path=db) is not None
    quality = telemetry._usage_report(root, days=1, limit=20, usage_db_path=db)["current_native_search_quality"]
    assert quality["count"] == 4
    assert {row["route_outcome"]: row["count"] for row in quality["route_outcomes"]} == {
        "promoted_existing": 1, "promoted_retry": 1, "applicability_vetoed": 1}
    assert quality["route_changes"] == 2


@pytest.mark.parametrize("ranges", ["full", "excerpt", "multiple"])
def test_cached_validation_reads_each_receipt_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ranges: str,
) -> None:
    cfg = config(tmp_path)
    result, promotion = result_packet(cfg, ranges=ranges)
    reads: list[str] = []
    original = shadow_sources.reread_source

    def counted(cfg: Config, receipt: dict[str, Any]) -> dict[str, Any]:
        reads.append(receipt["id"])
        return original(cfg, receipt)

    monkeypatch.setattr(shadow_sources, "reread_source", counted)
    assert applicability.valid_result(cfg, result, promotion, time.time())
    assert reads == [TARGET]


@pytest.mark.parametrize("field", ["id", "type", "locator", "sha256", "start_line", "end_line",
                                   "total_lines", "file_bytes", "complete", "ranges", "extra",
                                   "empty", "too_many", "citation", "bytes", "missing"])
def test_one_pass_validation_still_rejects_invalid_receipts(tmp_path: Path, field: str) -> None:
    cfg = config(tmp_path)
    result, promotion = result_packet(cfg, ranges="multiple")
    result = copy.deepcopy(result)
    receipt = result["sources"][0]
    if field in {"id", "type", "locator", "sha256"}:
        receipt[field] = "invalid"
    elif field in {"start_line", "end_line", "total_lines", "file_bytes"}:
        receipt[field] += 1
    elif field == "complete":
        receipt[field] = not receipt[field]
    elif field == "ranges":
        receipt[field][0]["start_line"] = 0
    elif field == "extra":
        receipt["extra"] = True
    elif field == "empty":
        result["sources"] = []
    elif field == "too_many":
        result["sources"] *= shadow_sources.MAX_SOURCES + 1
    elif field == "citation":
        result["citations"][0]["start_line"] = 2  # Not in the inspected ranges.
        result["citations"][0]["end_line"] = 2
    elif field == "bytes":
        (cfg.source_root / "docs" / "atlas.md").write_text("# Changed bytes\n")
    elif field == "missing":
        (cfg.source_root / "docs" / "atlas.md").unlink()
    assert not applicability.valid_result(cfg, result, promotion, time.time())
