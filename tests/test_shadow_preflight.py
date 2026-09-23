"""Synthetic provider/corpus checks, not historical replay or model efficacy evidence."""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_local_knowledge import index, shadow, shadow_sources
from hermes_local_knowledge.config import Config
from tests.test_shadow import Model, capture, cfg as shadow_cfg, complete, mutate, rows

cfg = shadow_cfg

BASE = "runbook:docs-quartz-backup"


@pytest.mark.parametrize("failure", ["oversized", "missing", "unreadable", "unsupported", "total_bytes", "count", "absent"])
def test_ineligible_complete_baseline_zero_model_calls(cfg: Config, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    # Seed a real reusable receipt with the synthetic provider before changing the baseline.
    assert complete(cfg)["verified"] == 1
    baseline = [BASE]
    target = cfg.source_root / "docs" / "quartz-backup.md"
    expected = {"oversized": "source_too_large", "missing": "source_unavailable",
                "unreadable": "source_unavailable", "unsupported": "unsupported_source",
                "total_bytes": "source_bytes_budget", "count": "source_count_budget",
                "absent": "missing_source"}[failure]
    if failure == "oversized":
        target.write_text("x" * 24_001)
    elif failure == "missing":
        target.unlink()
    elif failure == "unreadable":
        real_read = shadow_sources._read_source_bytes
        def denied(path: Any) -> bytes:
            if path == target:
                raise PermissionError("SECRET filesystem details")
            return real_read(path)
        monkeypatch.setattr(shadow_sources, "_read_source_bytes", denied)
    elif failure == "unsupported":
        (cfg.source_root / "scripts").mkdir()
        (cfg.source_root / "scripts" / "quartz.py").write_text('"""Quartz helper."""\n')
        index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
        baseline.append("script:scripts-quartz-py")
    elif failure == "absent":
        baseline.append("runbook:absent")
    else:
        for n in range(9 if failure == "count" else 5):
            (cfg.source_root / "docs" / f"extra-{n}.md").write_text("x" * (20_000 if failure == "total_bytes" else 40))
            baseline.append(f"runbook:docs-extra-{n}")
        index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    event = capture(cfg, user_request="Locate the Quartz service restart runbook.", baseline_ids=baseline)
    shadow.finish_session(cfg, "session-1")
    model = Model()
    assert shadow.run_batch(cfg, llm=model)["unresolved"] == 1
    assert model.calls == []  # Includes reuse applicability, not only investigator calls.
    saved = next(row for row in rows(cfg) if row["id"] == event["case_id"])
    assert saved["calls"] == 0
    assert saved["reason"] == "ineligible_baseline_" + expected
    assert json.loads(saved["baseline_ids"]) == baseline
    receipt = json.loads(saved["diagnostics"])
    assert receipt["eligibility"] == "ineligible"
    assert [item["id"] for item in receipt["attempts"]] == baseline
    assert any(item["reason"] == expected for item in receipt["attempts"])
    assert "SECRET" not in saved["diagnostics"]
    assert "Quartz" not in json.dumps(shadow.report(cfg))


@pytest.mark.parametrize("category", [None, "baseline_coverage", "SECRET raw explanation", ["ambiguous_lookup"]])
def test_model_abstention_category_is_bounded_and_backward_compatible(cfg: Config, category: Any) -> None:
    class Abstains:
        def complete_structured(self, **kwargs: Any) -> Any:
            return SimpleNamespace(parsed={"action": "unresolved", "category": category, "reason": "SECRET"})
    assert complete(cfg, Abstains())["unresolved"] == 1
    row = rows(cfg)[0]
    receipt = json.loads(row["diagnostics"])
    assert row["reason"] == "insufficient_evidence"
    assert receipt["eligibility"] == "eligible"
    expected = "baseline_coverage" if category == "baseline_coverage" else "unspecified"
    assert receipt["abstention_category"] == expected
    assert receipt["actions"] == {"investigator:unresolved": 1}
    assert shadow.report(cfg)["diagnostics"]["abstention_categories"] == {expected: 1}
    assert "SECRET" not in row["diagnostics"]


def test_success_and_provider_failure_retain_structural_receipts(cfg: Config) -> None:
    assert complete(cfg)["verified"] == 1
    receipt = json.loads(rows(cfg)[0]["diagnostics"])
    assert receipt["eligibility"] == "eligible"
    assert receipt["actions"] == {"investigator:read": 1, "investigator:propose": 1, "verifier:verified": 1}
    assert {a["phase"] for a in receipt["attempts"]} == {"preflight", "acquisition", "verifier"}
    assert "Restart Quartz safely" not in json.dumps(receipt)
    capture(cfg, user_request="Locate the Quartz restart procedure now.")
    shadow.finish_session(cfg, "session-1")
    class Fails:
        def complete_structured(self, **kwargs: Any) -> Any:
            raise RuntimeError("SECRET provider text")
    assert shadow.run_batch(cfg, llm=Fails())["unresolved"] == 1
    failed = next(row for row in rows(cfg) if row["status"] == "unresolved")
    assert failed["reason"] == "interrupted_ambiguous"
    assert json.loads(failed["diagnostics"])["eligibility"] == "eligible"
    assert "SECRET" not in repr(failed)


def test_diagnostics_bounded_with_repeated_reads(cfg: Config) -> None:
    cfg = replace(cfg, verified_routing=replace(cfg.verified_routing, max_model_calls_per_case=24))
    class Repeats:
        def complete_structured(self, **kwargs: Any) -> Any:
            return SimpleNamespace(parsed={"action": "read", "ids": [BASE] * 3})
    assert complete(cfg, Repeats())["unresolved"] == 1
    receipt = json.loads(rows(cfg)[0]["diagnostics"])
    assert receipt["read_attempts"] == 70
    assert len(receipt["attempts"]) == 64
    assert receipt["attempts_truncated"] is True
    assert len(json.dumps(receipt)) < 50_000
    assert rows(cfg)[0]["reason"] == "call_budget"
    assert shadow.report(cfg)["diagnostics"]["truncated_cases"] == 1


def test_unknown_model_read_id_not_persisted_as_prose(cfg: Config) -> None:
    class Unknown:
        def complete_structured(self, **kwargs: Any) -> Any:
            return SimpleNamespace(parsed={"action": "read", "ids": ["SECRET arbitrary model text"]})
    assert complete(cfg, Unknown())["unresolved"] == 1
    row = rows(cfg)[0]
    assert row["reason"] == "unsearched_source"
    assert "SECRET" not in row["diagnostics"]
    assert json.loads(row["diagnostics"])["attempts"][-1] == {
        "id": "", "phase": "acquisition", "reason": "unsearched_source"}


def test_legacy_report_does_not_migrate_and_write_adds_column(cfg: Config) -> None:
    capture(cfg)
    mutate(cfg, "ALTER TABLE cases DROP COLUMN diagnostics")
    with shadow._connect(cfg) as conn:
        before = conn.execute("SELECT * FROM cases").fetchone()
    report = shadow.report(cfg)
    assert report["diagnostics"]["unavailable"] == 1
    with shadow._connect(cfg) as conn:
        assert "diagnostics" not in {r[1] for r in conn.execute("PRAGMA table_info(cases)")}
    shadow.finish_session(cfg, "session-1")
    after = rows(cfg)[0]
    assert after["diagnostics"] == "{}"
    assert after["id"] == before["id"] and after["baseline_ids"] == before["baseline_ids"]
    assert shadow.run_batch(cfg, llm=Model())["verified"] == 1
    assert shadow.report(cfg)["diagnostics"]["available"] == 1


def test_lease_loss_cannot_publish_diagnostics(cfg: Config) -> None:
    class Steals:
        def complete_structured(self, **kwargs: Any) -> Any:
            mutate(cfg, "UPDATE lease SET owner='new-owner'")
            mutate(cfg, "UPDATE cases SET diagnostics='{}'")
            return SimpleNamespace(parsed={"action": "unresolved", "category": "baseline_coverage"})
    assert complete(cfg, Steals())["error"] == "worker_error"
    assert rows(cfg)[0]["diagnostics"] == "{}"
    assert rows(cfg)[0]["status"] == "running"


def test_preflight_source_change_before_first_model_call_blocks_spend(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    real_applicable = shadow._applicable
    def changed(*args: Any, **kwargs: Any) -> Any:
        target = cfg.source_root / "docs" / "quartz-backup.md"
        target.write_text(target.read_text() + "Changed coverage\n")
        return real_applicable(*args, **kwargs)
    monkeypatch.setattr(shadow, "_applicable", changed)
    model = Model()
    assert complete(cfg, model)["unresolved"] == 1
    assert model.calls == []
    assert rows(cfg)[0]["reason"] == "source_changed_during_verification"


def test_preflight_time_exhaustion_makes_no_model_call(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(shadow.time, "monotonic", lambda: clock[0])
    real_read = shadow_sources._read_source_bytes
    def slow(path: Any) -> bytes:
        value = real_read(path)
        clock[0] += cfg.verified_routing.max_worker_seconds + 1
        return value
    monkeypatch.setattr(shadow_sources, "_read_source_bytes", slow)
    model = Model()
    assert complete(cfg, model) == {"claimed": 1, "verified": 0, "unresolved": 0}
    assert model.calls == []
    # Preserve the existing safe zero-call retry contract after a batch timeout.
    assert rows(cfg)[0]["status"] == "pending"
    assert json.loads(rows(cfg)[0]["diagnostics"])["attempts"]


def test_unread_baseline_change_during_verifier_cannot_publish(cfg: Config) -> None:
    target = cfg.source_root / "docs" / "zebra.md"
    target.write_text("# Gardening\nA source unrelated to Quartz.\n")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    capture(cfg, baseline_ids=[BASE, "runbook:docs-zebra"])
    shadow.finish_session(cfg, "session-1")
    class Changes(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            result = super().complete_structured(**kwargs)
            if kwargs["purpose"].endswith("verifier"):
                packet = json.loads(kwargs["input"][0]["text"])
                assert "runbook:docs-zebra" not in {s["id"] for s in packet["sources"]}
                target.write_text("# Changed scope\nNew useful evidence.\n")
            return result
    assert shadow.run_batch(cfg, llm=Changes())["unresolved"] == 1
    row = rows(cfg)[0]
    assert row["reason"] == "source_changed_during_verification"
    assert row["status"] == "unresolved" and row["verified_at"] == 0
    # Retain the diagnostic proposal, never promote it to verified evidence.
    assert json.loads(row["result"])["provenance"] == "ai_proposed"
    assert json.loads(row["diagnostics"])["actions"]["verifier:verified"] == 1

