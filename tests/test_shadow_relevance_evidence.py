"""Synthetic receipt/selection contracts, not evidence of model relevance quality."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.test_shadow_applicability import (
    ATLAS, BASELINE, LOOKUP, WORKFLOW, ScriptedModel, cfg as cfg,
    observe, records, run,
)

from hermes_local_knowledge import index, shadow, shadow_sources
from hermes_local_knowledge.config import Config
from hermes_local_knowledge.shadow_sources import Diagnostics, Evidence, checked_citations


@pytest.mark.parametrize("kind", ["script", "large", "unavailable", "unsupported"])
def test_irrelevant_lead_does_not_veto_useful_route(cfg: Config, kind: str) -> None:
    path = cfg.source_root / "docs" / "gardening.md"
    path.write_text("# Gardening unrelated to Atlas\n" + "Plant care.\n" * 4000)
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    ident = "runbook:docs-gardening"
    if kind == "script":
        scripts = cfg.source_root / "scripts"
        scripts.mkdir()
        path = scripts / "gardening.py"
        path.write_text('"""Gardening plant care unrelated to hosts."""\n')
        index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
        ident = "script:scripts-gardening-py"
    elif kind == "unavailable":
        path.unlink()
    elif kind == "unsupported":
        with closing(sqlite3.connect(cfg.state_dir / "index.sqlite")) as conn, conn:
            conn.execute("UPDATE artifacts SET type='cron_job' WHERE id=?", (ident,))
    observe(cfg, baseline=[*BASELINE, ident])
    model = ScriptedModel()
    assert run(cfg, model)["verified"] == 1
    packet = json.loads(model.calls[0]["input"][0]["text"])
    candidate = next(c for c in packet["candidates"] if c["id"] == ident)
    assert candidate["read_status"] == {
        "script": "available", "large": "excerpt_required", "unavailable": "source_unavailable",
        "unsupported": "unsupported_source",
    }[kind]
    result = json.loads(records(cfg)[0]["result"])
    assert result["baseline_review"][-1]["disposition"] == "not_useful"
    assert ident not in {s["id"] for s in result["sources"]}


@pytest.mark.parametrize("script", ["", "py", "cjs", "mjs"])
def test_selected_large_source_exact_excerpt_verified_and_reused(cfg: Config, script: str) -> None:
    padding = "# unrelated historical note " + "x" * 130 + "\n"
    if script:
        (cfg.source_root / "scripts").mkdir()
        suffix = script
        path = cfg.source_root / "scripts" / ("atlas-inventory." + suffix)
        body = ("def atlas_inventory():\n    return ['scheduler', 'tracker']\n" if script == "py" else
                "// Atlas inventory\nconst inventory = ['scheduler', 'tracker'];\n")
        ident = "script:scripts-atlas-inventory-" + suffix
    else:
        path = cfg.source_root / "docs" / "atlas-inventory.md"
        body = "# Atlas inventory\nAtlas inventory lists scheduler and tracker.\n"
        ident = ATLAS
    path.write_text(padding * 200 + body + padding * 10)
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    observe(cfg, baseline=[WORKFLOW, ident])

    class ExcerptModel(ScriptedModel):
        def complete_structured(self, **kwargs: Any) -> Any:
            packet = json.loads(kwargs["input"][0]["text"])
            source = next((s for s in packet["sources"] if s["id"] == ident), None)
            answer: dict[str, Any]
            if source is None:
                location = next((s for s in packet["locations"] if s["id"] == ident), None)
                if location is None:
                    answer = {"action": "locate_source", "id": ident, "query": "inventory"}
                else:
                    start = location["matches"][0]["line"]
                    answer = {"action": "read_excerpt", "id": ident, "start_line": start, "end_line": start + 1}
            else:
                assert source["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
                assert source["start_line"] == 201 and source["end_line"] == 202
                assert source["total_lines"] == 212 and source["complete"] is False
                assert source["lines"][0].startswith("201: ") and len(source["lines"]) == 2
                assert "historical" not in " ".join(source["lines"])
                citation = {key: source[key] for key in ("id", "locator", "sha256")}
                citation.update(start_line=201, end_line=202)
                review = [{"id": WORKFLOW, "disposition": "not_useful", "covered_by": [],
                           "reason": "Generic workflow, not inventory."},
                          {"id": ident, "disposition": "retained", "covered_by": [ident],
                           "reason": "Direct inventory evidence in the selected lines.",
                           "evidence_scope": "useful_evidence_only"}]
                if kwargs["purpose"].endswith("applicability"):
                    answer = {"verdict": "applicable", "case_id": packet["stored_routes"][0]["case_id"],
                              "route_ids": [ident], "lookup_supported": True,
                              "baseline_review": review, "citations": [citation]}
                elif kwargs["purpose"].endswith("verifier"):
                    competitor = next(s for s in packet["sources"] if s["id"] == packet["near_miss_id"])
                    near_cite = {key: competitor[key] for key in ("id", "locator", "sha256")}
                    near_cite.update(start_line=1, end_line=2)
                    answer = {"verdict": "verified", "route_ids": [ident],
                              "near_miss_id": competitor["id"], "task_supported": True,
                              "lookup_supported": True, "near_miss_rejected": True,
                              "baseline_review": review, "citations": [citation, near_cite]}
                else:
                    answer = {"action": "propose", "route_ids": [ident], "citations": [citation]}
            return SimpleNamespace(parsed=answer, usage={})

    assert run(cfg, ExcerptModel())["verified"] == 1
    result = json.loads(records(cfg)[0]["result"])
    assert result["baseline_review"][1]["evidence_scope"] == "useful_evidence_only"
    assert observe(cfg, baseline=[WORKFLOW, ident])["observation"] == "would_reuse"
    observe(cfg, baseline=[WORKFLOW, ident], lookup={**LOOKUP, "intent": "Locate Atlas current inventory"})
    assert run(cfg, ExcerptModel())["would_reuse"] == 1
    # Drift outside the excerpt still invalidates the full-file identity.
    path.write_text(path.read_text().replace("historical", "superseded", 1))
    assert observe(cfg, baseline=[WORKFLOW, ident])["observation"] == "fallback"


def test_unread_useful_baseline_cannot_verify(cfg: Config) -> None:
    path = cfg.source_root / "docs" / "atlas-extra.md"
    path.write_text("# Atlas useful inventory additions\n" + "New services.\n" * 4000)
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    extra = "runbook:docs-atlas-extra"
    observe(cfg, baseline=[*BASELINE, extra])

    class UnsupportedCoverage(ScriptedModel):
        def complete_structured(self, **kwargs: Any) -> Any:
            response = super().complete_structured(**kwargs)
            if kwargs["purpose"].endswith("verifier"):
                item = next(r for r in response.parsed["baseline_review"] if r["id"] == extra)
                item.update(disposition="equivalent", covered_by=[ATLAS], reason="Claim without inspection")
            return response

    assert run(cfg, UnsupportedCoverage())["unresolved"] == 1
    assert records(cfg)[0]["reason"] == "baseline_coverage_lost"


def test_excerpt_citations_and_baseline_scope_cannot_claim_unread_lines(cfg: Config) -> None:
    evidence = Evidence(cfg, "")
    evidence.include([ATLAS])
    source = evidence.read(ATLAS, start_line=2, end_line=2)
    citation = {key: source[key] for key in ("id", "locator", "sha256")}
    citation.update(start_line=1, end_line=2)
    with pytest.raises(ValueError, match="invalid_citation_range"):
        checked_citations([citation], evidence.sources, [ATLAS])
    citation.update(start_line=2)
    parsed = {"baseline_review": [{"id": ATLAS, "disposition": "retained", "covered_by": [ATLAS],
                                  "reason": "Useful excerpt", "evidence_scope": "complete_content"}]}
    with pytest.raises(ValueError, match="baseline_coverage_unknown"):
        shadow._coverage(parsed, {"baseline_ids": [ATLAS]}, evidence, [ATLAS], [citation])
    with pytest.raises(ValueError, match="invalid_read_range"):
        evidence.read(ATLAS, start_line=True, end_line=2)
    with pytest.raises(ValueError, match="invalid_read_range"):
        evidence.read(ATLAS, start_line=1, end_line=161)


@pytest.mark.parametrize("assignment", ["API_TOKEN", "AWS_ACCESS_KEY_ID", "AZURE_ACCESS_KEY"])
def test_script_credential_refusal_preserves_exact_content_boundary(cfg: Config, assignment: str) -> None:
    (cfg.source_root / "scripts").mkdir()
    path = cfg.source_root / "scripts" / "atlas-secret.py"
    path.write_text(f"# Atlas\n{assignment} = 'SYNTHETIC_TEST_ONLY'\nprint('inventory')\n")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    evidence = Evidence(cfg, "")
    ident = "script:scripts-atlas-secret-py"
    evidence.include([ident])
    assert not evidence.try_read(ident, start_line=3, end_line=3)
    assert evidence.refusals[ident] == "credential_source"
    assert "SYNTHETIC_TEST_ONLY" not in str(evidence.packet())


def test_cost_order_keeps_individually_fitting_reuse(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh eight-dependency candidate fits alone but must not crowd out a cheap viable route.
    for n in range(8):
        (cfg.source_root / "docs" / f"old-{n}.md").write_text(f"# Atlas history {n}\nHistorical inventory.\n")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    observe(cfg)
    assert run(cfg, ScriptedModel())["verified"] == 1
    old = records(cfg)[0]
    oversized = dict(old)
    evidence = Evidence(cfg, "")
    ids = [f"runbook:docs-old-{n}" for n in range(8)]
    evidence.include(ids)
    oversized["result"] = json.dumps({"route_ids": ids[:1],
        "sources": [shadow_sources.identity(evidence.read(i)) for i in ids]})
    oversized["id"] = "expensive"
    monkeypatch.setattr(shadow, "_reuse_candidates", lambda *args: [oversized, old])
    observe(cfg, lookup={**LOOKUP, "intent": "Locate Atlas inventory and progress"})
    model = ScriptedModel()
    assert run(cfg, model)["would_reuse"] == 1
    packet = json.loads(model.calls[0]["input"][0]["text"])
    assert [c["case_id"] for c in packet["stored_routes"]] == [old["id"]]


def test_diagnostics_bounds_migration_and_malformed_receipts(cfg: Config) -> None:
    observe(cfg)
    assert run(cfg, ScriptedModel())["verified"] == 1
    row = records(cfg)[0]
    receipt = json.loads(row["diagnostics"])
    assert receipt["actions"] == {"investigator:read": 1, "investigator:propose": 1, "verifier:verified": 1}
    diagnostics = Diagnostics()
    for _ in range(100):
        diagnostics.read("arbitrary model prose", "acquisition", "source_unavailable", known=False)
    packet = diagnostics.packet()
    assert len(packet["attempts"]) == 64 and packet["attempts_truncated"] is True
    assert all(item["id"] == "" for item in packet["attempts"])
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        conn.execute("ALTER TABLE cases DROP COLUMN diagnostics")
    assert shadow.report(cfg)["diagnostics"]["unavailable"] == 1
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        assert conn.execute("SELECT diagnostics FROM cases").fetchone()[0] == "{}"
    malformed = ["{", "[]", "null", json.dumps({"version": True}),
                 json.dumps({"version": 1, "actions": [], "attempts": [None, "x", {"reason": []}]}),
                 json.dumps({"version": 1, "actions": {"verifier:verified": "secret", "investigator:read": -1,
                             "investigator:propose": True, "arbitrary prose": 1}, "attempts": {}})]
    for value in malformed:
        with closing(shadow._connect(cfg, create=True)) as conn, conn:
            conn.execute("UPDATE cases SET diagnostics=?", (value,))
        report = shadow.report(cfg)
        assert "error" not in report
        assert report["model_calls"] == 3
        assert report["diagnostics"]["actions"] == {}
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        conn.execute("UPDATE cases SET diagnostics=?", (json.dumps(packet),))
    report = shadow.report(cfg)
    assert report["diagnostics"]["refusals_retained"] == {"source_unavailable": 64}
    assert report["diagnostics"]["truncated_cases"] == 1


@pytest.mark.parametrize("mode", ["file_limit", "selected_limit", "symlink_swap"])
def test_excerpt_limits_and_confinement(cfg: Config, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    path = cfg.source_root / "docs/atlas-inventory.md"
    if mode == "file_limit":
        path.write_text("x" * (shadow_sources.MAX_FILE_BYTES + 1) + "\n")
        with pytest.raises(ValueError, match="source_too_large"):
            shadow_sources.read_source(cfg, ATLAS, start_line=1, end_line=1)
    elif mode == "selected_limit":
        path.write_text("x" * (shadow_sources.MAX_SOURCE_BYTES + 1) + "\n")
        with pytest.raises(ValueError, match="source_too_large"):
            shadow_sources.read_source(cfg, ATLAS, start_line=1, end_line=1)
    else:
        outside = tmp_path / "outside.md"
        outside.write_text("SYNTHETIC OUTSIDE CONTENT\n")
        original = shadow_sources._source_path

        def swap(config: Config, row: dict[str, Any]) -> Path:
            approved = original(config, row)
            path.unlink()
            try:
                path.symlink_to(outside)
            except OSError:
                pytest.skip("symlink creation unavailable")
            return approved

        monkeypatch.setattr(shadow_sources, "_source_path", swap)
        with pytest.raises((OSError, ValueError)):
            shadow_sources.read_source(cfg, ATLAS, start_line=1, end_line=1)


def test_equal_count_large_receipt_union_cannot_starve_lower_byte_route(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both stored cases have three receipts. Source count alone cannot decide
    # admission: their combined bytes exceed the model-packet limit.
    for name in ["atlas-inventory", "atlas-tracker", "maintenance-workflow"]:
        path = cfg.source_root / "docs" / f"{name}.md"
        path.write_text(path.read_text() + "z" * 12_000 + "\n")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    observe(cfg)
    assert run(cfg, ScriptedModel())["verified"] == 1
    good = records(cfg)[0]
    result = json.loads(good["result"])
    sources = []
    for i in range(3):
        path = cfg.source_root / "docs" / f"expensive-{i}.md"
        path.write_text("# Inventory supporting evidence\n" + "x" * 23_000 + "\n")
        sources.append(f"runbook:docs-expensive-{i}")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    evidence = shadow_sources.Evidence(cfg, "")
    evidence.include(sources)
    large = dict(result, route_ids=sources[:1],
                 sources=[shadow_sources.identity(evidence.read(i)) for i in sources])
    now = time.time()
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        conn.execute("UPDATE cases SET verified_at=? WHERE id=?", (now - 2, good["id"]))
        columns = [r[1] for r in conn.execute("PRAGMA table_info(cases)")]
        clone = dict(good, id="expensive", verified_at=now - 1, result=json.dumps(large))
        conn.execute(f"INSERT INTO cases ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                     [clone[k] for k in columns])
    shortlist = {r["id"]: r for r in records(cfg)}
    monkeypatch.setattr(shadow, "_reuse_candidates",
                        lambda *args: [shortlist["expensive"], shortlist[good["id"]]])
    observe(cfg, lookup={**LOOKUP, "intent": LOOKUP["intent"] + " with three records"})

    class Reuse(ScriptedModel):
        def complete_structured(self, **kwargs: Any) -> Any:
            packet = json.loads(kwargs["input"][0]["text"])
            assert [c["case_id"] for c in packet["stored_routes"]] == [good["id"]]
            return super().complete_structured(**kwargs)

    assert run(cfg, Reuse())["would_reuse"] == 1


def test_full_read_upgrades_partial_cache(cfg: Config) -> None:
    evidence = Evidence(cfg, "")
    evidence.include([ATLAS])
    partial = evidence.read(ATLAS, start_line=2, end_line=2)
    whole = evidence.read(ATLAS)
    assert whole is not partial
    assert whole.get("complete", True)
    assert set(shadow_sources.exact_lines(whole)) == {1, 2}
    citation = {key: whole[key] for key in ("id", "locator", "sha256")}
    citation.update(start_line=1, end_line=2)
    assert checked_citations([citation], evidence.sources, [ATLAS])


def test_multiple_exact_ranges_survive_refresh_reuse_and_reject_gaps(cfg: Config) -> None:
    path = cfg.source_root / "docs" / "atlas-inventory.md"
    path.write_text("# Atlas inventory\n" + "Unrelated.\n" * 200 + "Atlas progress tracker.\n")
    evidence = Evidence(cfg, "")
    evidence.include([ATLAS])
    evidence.read(ATLAS, start_line=1, end_line=1)
    source = evidence.read(ATLAS, start_line=202, end_line=202)
    assert set(shadow_sources.exact_lines(source)) == {1, 202}
    citation = {key: source[key] for key in ("id", "locator", "sha256")}
    citations = [{**citation, "start_line": n, "end_line": n} for n in (1, 202)]
    assert checked_citations(citations, evidence.sources, [ATLAS]) == citations
    with pytest.raises(ValueError, match="invalid_citation_range"):
        checked_citations([{**citation, "start_line": 1, "end_line": 2}], evidence.sources, [ATLAS])
    receipt = shadow_sources.identity(source)
    assert shadow_sources.sources_current(cfg, [receipt])
    refreshed = evidence.read(ATLAS, refresh=True)
    assert shadow_sources.identity(refreshed) == receipt
    restored = Evidence(cfg, "")
    restored.include([ATLAS])
    assert shadow_sources.identity(restored.read(ATLAS, receipt=receipt)) == receipt
    path.write_text(path.read_text().replace("Unrelated.", "Changed.", 1))
    assert not shadow_sources.sources_current(cfg, [receipt])


def test_navigation_locations_are_not_citable_and_detect_drift(cfg: Config) -> None:
    evidence = Evidence(cfg, "")
    evidence.include([ATLAS])
    evidence.locate(ATLAS, "inventory")
    location = evidence.packet()["locations"][0]
    assert location["matches"] and location["total_lines"] == 2
    assert "text" not in location and "lines" not in location
    assert not evidence.sources
    path = cfg.source_root / "docs" / "atlas-inventory.md"
    path.write_text(path.read_text() + "New facts.\n")
    assert not evidence.try_read(ATLAS, start_line=1, end_line=2)
    assert evidence.refusals[ATLAS] == "source_identity_changed"


def test_refused_navigation_counts_toward_scan_budget(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = Evidence(cfg, "")
    evidence.include([ATLAS])
    attempts = []

    def refused(*args: Any) -> Any:
        attempts.append(1)
        raise ValueError("credential_source")

    monkeypatch.setattr(shadow_sources, "locate_source", refused)
    for _ in range(6):
        evidence.locate(ATLAS, "inventory")
    with pytest.raises(ValueError, match="location_budget"):
        evidence.locate(ATLAS, "inventory")
    assert len(attempts) == 6 and not evidence.locations


def test_large_excerpt_near_miss_reaches_verifier(cfg: Config) -> None:
    path = cfg.source_root / "docs" / "maintenance-workflow.md"
    path.write_text(path.read_text() + "Unrelated old note.\n" * 3000)
    for other in (cfg.source_root / "docs").glob("*.md"):
        if other.name not in {"maintenance-workflow.md", "atlas-inventory.md", "atlas-tracker.md"}:
            other.unlink()
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    observe(cfg)

    class ExcerptCompetitor(ScriptedModel):
        def complete_structured(self, **kwargs: Any) -> Any:
            packet = json.loads(kwargs["input"][0]["text"])
            available = {s["id"] for s in packet["sources"]}
            if kwargs["purpose"].endswith("investigator") and available and WORKFLOW not in available:
                return SimpleNamespace(parsed={"action": "read_excerpt", "id": WORKFLOW,
                                              "start_line": 1, "end_line": 2}, usage={})
            if kwargs["purpose"].endswith("verifier"):
                assert packet["near_miss_id"] == WORKFLOW
                assert next(s for s in packet["sources"] if s["id"] == WORKFLOW)["complete"] is False
            return super().complete_structured(**kwargs)

    model = ExcerptCompetitor()
    assert run(cfg, model)["verified"] == 1
    assert any(call["purpose"].endswith("verifier") for call in model.calls)


def test_contiguous_multi_range_complete_source_needs_no_partial_scope(cfg: Config) -> None:
    path = cfg.source_root / "docs" / "atlas-inventory.md"
    path.write_text("# Atlas inventory\n" + "Fact.\n" * 160)
    evidence = Evidence(cfg, "")
    evidence.include([ATLAS])
    assert not evidence.read(ATLAS, start_line=1, end_line=160)["complete"]
    source = evidence.read(ATLAS, start_line=161, end_line=161)
    assert source["complete"] and len(source["ranges"]) == 2
    review = [{"id": ATLAS, "disposition": "retained", "covered_by": [ATLAS], "reason": "All evidence inspected."}]
    citation = {**{k: source[k] for k in ("id", "locator", "sha256")}, "start_line": 1, "end_line": 1}
    assert shadow._coverage({"baseline_review": review}, {"baseline_ids": [ATLAS]},
                            evidence, [ATLAS], [citation])[0]["disposition"] == "retained"
    assert shadow_sources.sources_current(cfg, [shadow_sources.identity(source)])
