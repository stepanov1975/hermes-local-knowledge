"""Synthetic fixtures only; model seam doubles are not production responses."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.lab_benchmark import (
    Campaign, Corpus, GENERATION, JUDGMENT, PreparationBlocked, digest, encoded,
    private_write, read_json, select_spans, validate_drafts, validate_judgment,
)
from scripts.lab_corpus import frozen_manifest


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    root = tmp_path / "corpus"
    root.mkdir()
    text = "# Widget maintenance\nInspect widget backups. Restore widget archives. Audit widget health. Configure widget storage. Diagnose widget failures.\n"
    (root / "widget.md").write_text(text)
    logical = hashlib.sha256(json.dumps(["doc", "fixture", "widget.md", ""]).encode()).hexdigest()
    (root / "mapping.json").write_text(json.dumps([{
        "logical_id": logical, "id": "doc:widget", "locator": "widget.md",
        "source": "fixture", "type": "doc", "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }]))
    (root / "manifest.json").write_text(json.dumps({"inputs": frozen_manifest(root), "policy": "synthetic-fixture", "settings": {}}))
    return Corpus(root, hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest())


def contextual_corpus(tmp_path: Path) -> Corpus:
    """Invented task domains; no copied operational corpus content."""
    root = tmp_path / "context-corpus"
    root.mkdir()
    files = [
        ("cron_job", "cron:widget-update-acceptance", "jobs.json#fixture", json.dumps({"jobs": [{
            "id": "fixture", "skills": ["widget-operations"],
            "prompt": "Verify widget update acceptance. Read widget-update-report.md. Do not return only a plan."}]})),
        ("skill", "skill:plan", "skills/plan", "# Plan\nDraft a generic plan."),
        ("skill", "skill:widget-operations", "skills/widget-operations", "# Widget operations\nVerify widget update acceptance."),
        ("doc", "doc:widget-update-report", "docs/widget-update-report.md", "# Widget update\nCheck widget migration status."),
        ("tool_okf", "tool_okf:screen-vision", "tools/screen-vision.md", "related_tools:\n  - screen_snapshot\nInspect the rendered screen."),
        ("tool_okf", "tool_okf:screen-snapshot", "tools/screen-snapshot.md", "Read the screen structure."),
        ("mcp_server", "mcp:widget", "config.json#mcp_servers.widget", json.dumps({"mcp_servers": {"widget": {"command": "/opt/widget/launch.sh"}}})),
        *[("doc", f"doc:widget-{i:02d}", f"docs/widget-{i:02d}.md", f"Widget capability {i}.") for i in range(9)],
        *[("doc", f"doc:generic-update-{i}", f"docs/update-acceptance-{i}.md", "Generic framework update checks.") for i in range(4)],
    ]
    mapping = []
    for kind, artifact_id, locator, text in files:
        path = root / locator.split("#", 1)[0]
        if kind == "skill":
            path /= "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        mapping.append({"logical_id": hashlib.sha256(json.dumps([kind, "fixture", locator, ""]).encode()).hexdigest(),
                        "id": artifact_id, "locator": locator, "source": "fixture", "type": kind,
                        "content_sha256": hashlib.sha256(text.encode()).hexdigest()})
    (root / "mapping.json").write_text(json.dumps(mapping))
    (root / "manifest.json").write_text(json.dumps({"inputs": frozen_manifest(root), "policy": "synthetic-fixture", "settings": {}}))
    return Corpus(root, hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest())


def test_explicit_and_domain_relevant_frozen_evidence(tmp_path: Path) -> None:
    c = contextual_corpus(tmp_path)
    origins = {a["id"]: a["logical_id"] for a in c.artifacts}
    cron = c.packet(origins["cron:widget-update-acceptance"])
    assert {"skill:widget-operations", "doc:widget-update-report"} <= {s["artifact_id"] for s in cron["sources"]}
    assert "skill:plan" not in {s["artifact_id"] for s in cron["sources"]}  # Incidental prose isn't a declared skill reference.
    vision = c.packet(origins["tool_okf:screen-vision"])
    assert "tool_okf:screen-snapshot" in {s["artifact_id"] for s in vision["sources"]}
    mcp = c.packet(origins["mcp:widget"])
    assert "doc:widget-05" in {s["artifact_id"] for s in mcp["sources"]}
    expanded = c.packet(origins["mcp:widget"], expand=True)
    assert "doc:widget-08" in {s["artifact_id"] for s in expanded["sources"]}
    assert {s["logical_id"] for s in mcp["sources"]} <= {s["logical_id"] for s in expanded["sources"]}


def draft(packet: dict[str, Any], intent: str) -> dict[str, Any]:
    source = packet["sources"][0]
    return {"question": f"Where should I look to {intent} widget data?", "intent": intent,
            "targets": [{"logical_id": source["logical_id"], "evidence_id": source["spans"][0]["evidence_id"],
                         "quote": "widget", "rationale": "Fixture maintenance instructions"}]}


def decision(packet: dict[str, Any], index: int, case: dict[str, Any], *, associated: bool = True) -> dict[str, Any]:
    source = next(s for s in packet["sources"] if s["logical_id"] == packet["origin"])
    span = source["spans"][0]
    return {"index": index, "accept": associated, "reason": "Synthetic routing support, not complete answer text",
            "category": "accepted" if associated else "origin_mismatch", "origin_association": {
                "logical_id": packet["origin"], "supported": associated,
                "evidence_id": span["evidence_id"], "quote": span["text"],
                "rationale": "Synthetic origin task association" if associated else "Only the neighbor task matches"},
            "target_correctness": [{**target, "supported": True} for target in case["targets"]],
            "additional_targets": [],
            "goal_distinctness": {"distinct": True, "rationale": "Different user goals from accepted cases and earlier accepted drafts"}}


def test_semantic_origin_gate_is_independent_of_supported_nonorigin_target(tmp_path: Path) -> None:
    c = contextual_corpus(tmp_path)
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "cron:widget-update-acceptance")
    packet = c.packet(origin)
    target = next(s for s in packet["sources"] if s["artifact_id"] == "doc:widget-update-report")
    case = {"question": "Where do I inspect widget migration after its update?", "intent": "verify widget migration",
            "targets": [{"logical_id": target["logical_id"], "evidence_id": target["spans"][0]["evidence_id"],
                         "quote": "Check widget migration status.", "rationale": "Useful first read for update acceptance"}]}
    assert validate_drafts({"cases": [case]}, packet, 1) == [case]
    good = validate_judgment({"decisions": [decision(packet, 0, case)]}, [case], packet)
    assert good[0]["accept"]  # Origin relevance does NOT force origin target.
    unrelated = {**case, "question": "Where do I inspect a generic framework update?", "intent": "framework update"}
    assert validate_drafts({"cases": [unrelated]}, packet, 1) == [unrelated]  # Quote validity alone isn't semantics.
    bad = validate_judgment({"decisions": [decision(packet, 0, unrelated, associated=False)]}, [case], packet)
    assert not bad[0]["accept"] and bad[0]["category"] == "origin_mismatch"
    forged = decision(packet, 0, case)
    forged["origin_association"].update(evidence_id=target["spans"][0]["evidence_id"], quote="Check widget migration status.")
    with pytest.raises(ValueError, match="origin"):
        validate_judgment({"decisions": [forged]}, [case], packet)
    missing = decision(packet, 0, unrelated, associated=False)
    missing.update(accept=False, category="missing_evidence", reason="Origin context absent")
    assert validate_judgment({"decisions": [missing]}, [case], packet)[0]["category"] == "missing_evidence"


class SyntheticModel:
    """Semantic outcomes are supplied at the supported sender seam, not inferred lexically."""
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> dict[str, Any]:
        self.calls.append(prompt)
        if prompt.startswith(GENERATION):
            data = json.loads(prompt[len(GENERATION):])
            intents = ["inspect backups", "restore archives", "audit health", "configure storage", "diagnose failures"]
            used = {c["intent"] for c in data["accepted"]}
            output = {"cases": [draft(data["packet"], i) for i in intents if i not in used][:data["missing"]]}
        else:
            data = json.loads(prompt[len(JUDGMENT):])
            output = {"decisions": [decision(data["packet"], i, d) for i, d in enumerate(data["drafts"])]}
        return {"text": json.dumps(output), "usage": {"total_tokens": 10}, "provenance": {"synthetic_test_only": True}}


def test_schema_quotes_and_unsupported_labels(corpus: Corpus) -> None:
    packet = corpus.packet(next(iter(corpus.by_id)))
    c = draft(packet, "restore")
    assert validate_drafts({"cases": [c]}, packet, 5) == [c]
    c["targets"][0]["logical_id"] = "arbitrary-graph-neighbor"
    with pytest.raises(ValueError, match="Unsupported"):
        validate_drafts({"cases": [c]}, packet, 5)
    c = draft(packet, "restore")
    c["targets"][0]["quote"] = "invented support"
    with pytest.raises(ValueError):
        validate_drafts({"cases": [c]}, packet, 5)
    with pytest.raises(ValueError):
        validate_judgment({"decisions": [{"index": 0, "accept": "true", "reason": "yes"}]}, [c], packet)


def test_complete_resume_and_protected_fixed_partition(corpus: Corpus, tmp_path: Path) -> None:
    model = SyntheticModel()
    campaign = Campaign(corpus, tmp_path / "campaign", model, model)
    origins = list(corpus.by_id)
    assert campaign.prepare(origins)["accepted_cases"] == 5
    assert len(model.calls) == 2
    assert campaign.prepare(origins)["complete"]
    assert len(model.calls) == 2  # completed calls never repeated
    receipt = campaign.freeze()
    assert (receipt["development_cases"], receipt["validation_cases"]) == (3, 2)
    frozen = read_json(campaign.root / "frozen.json")
    dev = read_json(campaign.root / "worker-development/development.json")
    assert "validation" not in dev
    assert dev["cases"] == frozen["development"]
    hidden = {c["case_id"] for c in frozen["validation"]}
    assert not hidden & {c["case_id"] for c in dev["cases"]}
    assert sorted(c["case_id"] for c in frozen["development"] + frozen["validation"])[:3] == [c["case_id"] for c in dev["cases"]]
    assert campaign.freeze() == receipt
    for p in campaign.root.rglob("*"):
        assert p.stat().st_mode & 0o077 == 0


def test_prior_protocol_campaign_cannot_carry_labels_forward(corpus: Corpus, tmp_path: Path) -> None:
    root = tmp_path / "historical-campaign"
    campaign = Campaign(corpus, root, SyntheticModel(), SyntheticModel())
    campaign.prepare(list(corpus.by_id))
    accepted_path = root / "artifacts" / next(iter(corpus.by_id)) / "accepted.json"
    accepted_bytes = accepted_path.read_bytes()
    meta = json.loads((root / "campaign.json").read_text())
    meta["protocol"]["name"] = "lk-synthetic-routing-v3"
    (root / "campaign.json").write_text(json.dumps(meta))
    calls: list[str] = []

    def forbidden(prompt: str) -> dict[str, Any]:
        calls.append(prompt)
        raise AssertionError("Historical labels must require a new campaign")

    successor = Campaign(corpus, root, forbidden, forbidden)
    with pytest.raises(PreparationBlocked, match="Campaign identity/protocol drift"):
        successor.prepare(list(corpus.by_id))
    with pytest.raises(PreparationBlocked, match="Campaign identity/protocol drift"):
        successor.freeze()
    assert not calls and accepted_path.read_bytes() == accepted_bytes


def test_semantic_paraphrase_rejection_preserves_accepted_and_replaces(corpus: Corpus, tmp_path: Path) -> None:
    base = SyntheticModel()
    judgments = 0

    def model(prompt: str) -> dict[str, Any]:
        nonlocal judgments
        result = base(prompt)
        if prompt.startswith(GENERATION) and judgments == 0:
            value = json.loads(result["text"])
            value["cases"][1]["intent"] = value["cases"][0]["intent"]
            value["cases"][1]["question"] = "Which reference covers checking widget backup copies?"
            value["cases"][2]["intent"] = "configure unrelated firewall rules"
            value["cases"][2]["question"] = "Where do I configure firewall ingress rules?"
            result["text"] = json.dumps(value)
        if prompt.startswith(JUDGMENT):
            judgments += 1
            if judgments == 1:
                value = json.loads(result["text"])
                value["decisions"][1].update(accept=False, category="duplicate_intent", reason="Cosmetic paraphrase of an earlier intent")
                value["decisions"][1]["goal_distinctness"]["distinct"] = False
                value["decisions"][2].update(accept=False, category="unsupported_target", reason="Unsupported target for this specific intent")
                value["decisions"][2]["target_correctness"][0]["supported"] = False
                result["text"] = json.dumps(value)
        return result

    campaign = Campaign(corpus, tmp_path / "campaign", model, model)
    origin = next(iter(corpus.by_id))
    with pytest.raises(PreparationBlocked, match="quota unmet"):
        campaign.prepare([origin], rounds=1)
    assert len(campaign.accepted(origin)) == 3
    with pytest.raises(PreparationBlocked, match="incomplete"):
        campaign.freeze()
    assert campaign.prepare([origin])["accepted_cases"] == 5
    assert len(base.calls) == 4
    second = json.loads(base.calls[2][len(GENERATION):])
    assert len(second["accepted"]) == 3 and second["missing"] == 2
    assert {r["category"] for r in second["rejected"]} == {"duplicate_intent", "unsupported_target"}
    assert second["rejected"][0]["draft"]["question"] == "Which reference covers checking widget backup copies?"
    assert second["rejected"][1]["reason"] == "Unsupported target for this specific intent"


def test_origin_mismatch_regenerates_without_discarding_supported_alternatives(tmp_path: Path) -> None:
    c = contextual_corpus(tmp_path)
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "cron:widget-update-acceptance")
    calls: list[str] = []

    def model(prompt: str) -> dict[str, Any]:
        calls.append(prompt)
        if prompt.startswith(GENERATION):
            data = json.loads(prompt[len(GENERATION):])
            source = next(s for s in data["packet"]["sources"] if s["artifact_id"] == "doc:widget-update-report")
            if data["accepted"]:
                assert len(data["accepted"]) == 4
                assert data["rejected"][0]["category"] == "origin_mismatch"
                assert data["rejected"][0]["draft"]["question"] == "Where can I plan a generic framework update?"
                intents = ["review widget rollback"]
            else:
                intents = ["generic framework update", "inspect widget migration", "verify widget service", "check widget backup", "audit widget version"]
            obj = {"cases": [{"question": "Where can I plan a generic framework update?" if intent.startswith("generic") else f"Where do I {intent}?",
                              "intent": intent, "targets": [{"logical_id": source["logical_id"],
                              "evidence_id": source["spans"][0]["evidence_id"], "quote": "Check widget migration status.",
                              "rationale": "Synthetic routing support"}]} for intent in intents]}
        else:
            data = json.loads(prompt[len(JUDGMENT):])
            obj = {"decisions": [decision(data["packet"], i, d, associated=not d["intent"].startswith("generic"))
                                 for i, d in enumerate(data["drafts"])]}
        return {"text": json.dumps(obj), "provenance": {"synthetic_test_only": True}}

    campaign = Campaign(c, tmp_path / "campaign", model, model)
    with pytest.raises(PreparationBlocked, match="quota unmet"):
        campaign.prepare([origin], rounds=1)
    accepted = campaign.accepted(origin)
    assert len(accepted) == 4
    campaign.prepare([origin], rounds=1)
    assert campaign.accepted(origin)[:4] == accepted
    assert len(calls) == 4
    assert len(campaign.verify_cases(origin)) == 5
    assert all(case["targets"][0]["logical_id"] != origin for case in campaign.accepted(origin))


def test_missing_evidence_expands_on_resume_and_freeze_binds_each_packet(tmp_path: Path) -> None:
    c = contextual_corpus(tmp_path)
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "mcp:widget")
    prompts: list[str] = []

    def model(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        if prompt.startswith(GENERATION):
            data = json.loads(prompt[len(GENERATION):])
            packet = data["packet"]
            if packet["origin"] != origin:
                source = packet["sources"][0]
                intents = ["inventory", "storage", "migration", "boot", "access"]
            elif not data["accepted"]:
                source = next(s for s in packet["sources"] if s["artifact_id"] == "doc:widget-00")
                intents = ["inspect inventory", "find storage setup", "check migration", "diagnose boot", "review access"]
            else:
                assert any(r["category"] == "missing_evidence" for r in data["rejected"])
                source = next(s for s in packet["sources"] if s["artifact_id"] == "doc:widget-08")
                intents = ["review access"]
            output = {"cases": [{"question": f"Where do I {intent} for the widget server?", "intent": intent,
                                 "targets": [{"logical_id": source["logical_id"], "evidence_id": source["spans"][0]["evidence_id"],
                                              "quote": source["spans"][0]["text"], "rationale": "Synthetic whole-artifact routing support"}]}
                                for intent in intents]}
        else:
            data = json.loads(prompt[len(JUDGMENT):])
            verdicts = [decision(data["packet"], i, d) for i, d in enumerate(data["drafts"])]
            if data["packet"]["origin"] == origin and len(verdicts) == 5:
                verdicts[-1].update(accept=False, category="missing_evidence", reason="Access context absent from initial packet")
            output = {"decisions": verdicts}
        return {"text": json.dumps(output), "provenance": {"synthetic_test_only": True}}

    campaign = Campaign(c, tmp_path / "campaign", model, model)
    with pytest.raises(PreparationBlocked, match="quota unmet"):
        campaign.prepare([origin], rounds=1)
    first = campaign.accepted(origin)
    assert len(first) == 4
    assert campaign.prepare([origin], rounds=1)["accepted_cases"] == 5
    assert campaign.accepted(origin)[:4] == first
    assert len(prompts) == 4  # No regeneration of completed rounds on resume.
    assert len(campaign.verify_cases(origin)) == 5  # Both evidence selections retain receipt binding.
    campaign.prepare([o for o in c.by_id if o != origin])
    frozen = campaign.freeze()
    assert frozen["development_cases"] == 3 * len(c.by_id)
    assert frozen["validation_cases"] == 2 * len(c.by_id)


def test_invalid_quotes_supply_rejected_draft_and_structured_reason(corpus: Corpus, tmp_path: Path) -> None:
    base = SyntheticModel()
    generations = 0

    def model(prompt: str) -> dict[str, Any]:
        nonlocal generations
        response = base(prompt)
        if prompt.startswith(GENERATION):
            generations += 1
            if generations == 1:
                obj = json.loads(response["text"])
                obj["cases"][0]["targets"][0]["quote"] = "fabricated quotation"
                response["text"] = json.dumps(obj)
            else:
                data = json.loads(prompt[len(GENERATION):])
                assert data["rejected"][0]["category"] == "invalid_quote"
                assert data["rejected"][0]["draft"]["targets"][0]["quote"] == "fabricated quotation"
                assert data["rejected"][0]["reason"]
        return response

    campaign = Campaign(corpus, tmp_path / "campaign", model, model)
    assert campaign.prepare(list(corpus.by_id))["complete"]
    assert len(base.calls) == 4  # Valid siblings are judged before only the invalid draft is regenerated.


def test_interruption_after_generation_resumes_validation_only(corpus: Corpus, tmp_path: Path) -> None:
    base = SyntheticModel()

    def interrupted(prompt: str) -> dict[str, Any]:
        if prompt.startswith(JUDGMENT):
            raise KeyboardInterrupt
        return base(prompt)

    campaign = Campaign(corpus, tmp_path / "campaign", interrupted, interrupted)
    origin = next(iter(corpus.by_id))
    with pytest.raises(KeyboardInterrupt):
        campaign.prepare([origin])
    with pytest.raises(PreparationBlocked, match="Uncertain"):
        Campaign(corpus, campaign.root, base, base).prepare([origin])
    assert len(base.calls) == 1
    assert Campaign(corpus, campaign.root, base, base).prepare([origin], retry_uncertain=True)["complete"]
    assert len(base.calls) == 2 and base.calls[-1].startswith(JUDGMENT)
    received = read_json(campaign.root / "artifacts" / origin / "round-0000/generation.json")
    assert received["status"] == "received"  # original drafts are never lost


def test_protocol_and_corpus_identity_drift_fail_closed(corpus: Corpus, tmp_path: Path) -> None:
    campaign = Campaign(corpus, tmp_path / "campaign", SyntheticModel(), SyntheticModel())
    campaign.prepare(list(corpus.by_id))
    identity = read_json(campaign.root / "campaign.json")
    identity["protocol_digest"] = "changed"
    private_write(campaign.root / "campaign.json", identity)
    with pytest.raises(PreparationBlocked, match="identity/protocol"):
        campaign.freeze()
    (corpus.root / "widget.md").write_text("changed frozen bytes")
    with pytest.raises(ValueError, match="drift"):
        Corpus(corpus.root, corpus.manifest_sha256)


def test_large_source_evidence_is_selected_across_whole_file() -> None:
    text = "# heading\n" + "body\n" * 20000 + "# Important final procedure\nRestore final widget archive.\n"
    spans = select_spans(text)
    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)
    assert sum(end - start for start, end in spans) <= 24000
    assert len(spans) > 5
    assert any("Restore final widget archive" in text[start:end] for start, end in spans)


def test_malformed_judgment_retains_received_drafts_and_resumes(corpus: Corpus, tmp_path: Path) -> None:
    model = SyntheticModel()

    def malformed(prompt: str) -> dict[str, Any]:
        response = model(prompt)
        if prompt.startswith(JUDGMENT):
            response["text"] = '{"decisions":[]}'
        return response

    campaign = Campaign(corpus, tmp_path / "campaign", malformed, malformed)
    origin = next(iter(corpus.by_id))
    with pytest.raises(PreparationBlocked, match="Invalid judgment"):
        campaign.prepare([origin], rounds=1)
    assert len(model.calls) == 2
    assert Campaign(corpus, campaign.root, model, model).prepare([origin])["complete"]
    assert len(model.calls) == 3 and model.calls[-1].startswith(JUDGMENT)
    assert Campaign(corpus, campaign.root, model, model).freeze()["development_cases"] == 3


def test_case_identity_and_receipt_drift_cannot_freeze(corpus: Corpus, tmp_path: Path) -> None:
    campaign = Campaign(corpus, tmp_path / "campaign", SyntheticModel(), SyntheticModel())
    origin = next(iter(corpus.by_id))
    campaign.prepare([origin])
    cases = campaign.accepted(origin)
    cases[0]["question"] = "tampered question"
    private_write(campaign.root / "artifacts" / origin / "accepted.json", cases)
    with pytest.raises(PreparationBlocked, match="Case identity drift"):
        campaign.freeze()
    assert not (campaign.root / "worker-development").exists()


def test_registered_mcp_capability_links_require_exact_tool_namespace(tmp_path: Path) -> None:
    c = contextual_corpus(tmp_path)
    mapping = read_json(c.root / "mapping.json")
    for name, tool in [("widget-owned", "mcp__widget__inspect"), ("widget-decoy", "mcp__other__inspect_widget")]:
        locator = f"tools/{name}.md"
        text = f'---\ntool: "{tool}"\n---\nInspect widget state.\n'
        (c.root / locator).write_text(text)
        mapping.append({"logical_id": hashlib.sha256(json.dumps(["tool_okf", "fixture", locator, ""]).encode()).hexdigest(),
                        "id": f"tool_okf:{name}", "locator": locator, "source": "fixture", "type": "tool_okf",
                        "content_sha256": hashlib.sha256(text.encode()).hexdigest()})
    (c.root / "mapping.json").write_text(json.dumps(mapping))
    (c.root / "manifest.json").write_text(json.dumps({"inputs": frozen_manifest(c.root), "policy": "synthetic-fixture", "settings": {}}))
    c = Corpus(c.root, hashlib.sha256((c.root / "manifest.json").read_bytes()).hexdigest())
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "mcp:widget")
    packet = c.packet(origin, expand=True)
    sources = {s["artifact_id"]: s["logical_id"] for s in packet["sources"]}
    assert sources["tool_okf:widget-owned"] in packet["origin_capability_sources"]
    assert sources["tool_okf:widget-decoy"] not in packet["origin_capability_sources"]


def test_one_bad_quote_preserves_valid_drafts_and_only_regenerates_rejected(corpus: Corpus, tmp_path: Path) -> None:
    base = SyntheticModel()
    generation_count = 0

    def model(prompt: str) -> dict[str, Any]:
        nonlocal generation_count
        response = base(prompt)
        if prompt.startswith(GENERATION):
            generation_count += 1
            if generation_count == 1:
                obj = json.loads(response["text"])
                obj["cases"][-1]["targets"][0]["quote"] = "invented quotation"
                response["text"] = json.dumps(obj)
            else:
                data = json.loads(prompt[len(GENERATION):])
                assert len(data["accepted"]) == 4 and data["missing"] == 1
                assert data["rejected"][0]["category"] == "invalid_quote"
                assert data["rejected"][0]["draft"]["intent"] == "diagnose failures"
        return response

    campaign = Campaign(corpus, tmp_path / "campaign", model, model)
    origin = next(iter(corpus.by_id))
    with pytest.raises(PreparationBlocked, match="quota unmet"):
        campaign.prepare([origin], rounds=1)
    accepted = campaign.accepted(origin)
    assert len(accepted) == 4 and len(base.calls) == 2
    campaign.prepare([origin], rounds=1)
    assert campaign.accepted(origin)[:4] == accepted
    assert len(base.calls) == 4 and len(campaign.verify_cases(origin)) == 5
    assert campaign.freeze()["validation_cases"] == 2


def test_digest_encoding_literal_golden_vector() -> None:
    # Independent sha256sum of the documented UTF-8 bytes, NOT calculated by this test.
    assert encoded(["campaign", "origin", "question", "intent", []]) == b'["campaign","origin","question","intent",[]]'
    assert digest(["campaign", "origin", "question", "intent", []]) == "ddc83dcabcc747b76df43748796c24fb92fa3f701491789fbd8430057f69574f"


@pytest.mark.parametrize("associated", [True, False])
@pytest.mark.parametrize("defect", ["blank_quote", "blank_evidence", "blank_reason", "neighbor", "neighbor_span", "wrong_id"])
def test_origin_citations_bind_both_signs(tmp_path: Path, associated: bool, defect: str) -> None:
    c = contextual_corpus(tmp_path)
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "cron:widget-update-acceptance")
    packet = c.packet(origin)
    case = draft(packet, "inspect")
    verdict = decision(packet, 0, case, associated=associated)
    neighbor = packet["sources"][1]
    association = verdict["origin_association"]
    if defect.startswith("blank"):
        association[{"blank_quote": "quote", "blank_evidence": "evidence_id", "blank_reason": "rationale"}[defect]] = " "
    elif defect == "neighbor":
        association.update(logical_id=neighbor["logical_id"], evidence_id=neighbor["spans"][0]["evidence_id"], quote=neighbor["spans"][0]["text"])
    elif defect == "neighbor_span":
        association.update(evidence_id=neighbor["spans"][0]["evidence_id"], quote=neighbor["spans"][0]["text"])
    else:
        association["logical_id"] = "not-the-origin"
    before = encoded(verdict)
    with pytest.raises(ValueError, match="origin"):
        validate_judgment({"decisions": [verdict]}, [case], packet)
    assert encoded(verdict) == before  # No contradiction normalization or quote repair.


def test_constructive_additions_are_cited_unique_and_not_a_completeness_veto(tmp_path: Path) -> None:
    c = contextual_corpus(tmp_path)
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "cron:widget-update-acceptance")
    packet = c.packet(origin)
    case = draft(packet, "inspect")
    neighbor = packet["sources"][1]
    addition = {"logical_id": neighbor["logical_id"], "evidence_id": neighbor["spans"][0]["evidence_id"],
                "quote": neighbor["spans"][0]["text"], "rationale": "Useful first read for acceptance"}
    verdict = decision(packet, 0, case)
    # An available alternative is not mandatory: [] is a valid acceptance.
    assert validate_judgment({"decisions": [verdict]}, [case], packet) == [verdict]
    verdict["additional_targets"] = [addition]
    before = encoded([case, verdict])
    assert validate_judgment({"decisions": [verdict]}, [case], packet) == [verdict]
    assert encoded([case, verdict]) == before
    for additions in [None, [addition, addition], [case["targets"][0]],
                      [{**addition, "logical_id": "wrong"}], [{**addition, "evidence_id": "wrong"}],
                      [{**addition, "quote": "fabricated"}], [{**addition, "quote": ""}],
                      [{**addition, "rationale": ""}], [{**addition, "supported": True}]]:
        changed = {**verdict, "additional_targets": additions}
        with pytest.raises(ValueError):
            validate_judgment({"decisions": [changed]}, [case], packet)
    assert case["targets"] == [draft(packet, "inspect")["targets"][0]]


@pytest.mark.parametrize("defect", ["omit", "duplicate", "foreign", "neighbor_quote", "blank_negative", "accepted_negative", "bad_boolean"])
def test_correctness_covers_unique_proposed_ids(corpus: Corpus, defect: str) -> None:
    packet = corpus.packet(next(iter(corpus.by_id)))
    case = draft(packet, "inspect")
    verdict = decision(packet, 0, case)
    target = verdict["target_correctness"][0]
    if defect == "omit":
        verdict["target_correctness"] = []
    elif defect == "duplicate":
        verdict["target_correctness"].append(dict(target))
    elif defect == "foreign":
        target["logical_id"] = "foreign"
    elif defect == "neighbor_quote":
        target["evidence_id"] = "foreign-span"
    elif defect == "blank_negative":
        verdict.update(accept=False, category="unsupported_target")
        target.update(supported=False, quote="")
    elif defect == "accepted_negative":
        target["supported"] = False
    else:
        target["supported"] = 1
    with pytest.raises(ValueError):
        validate_judgment({"decisions": [verdict]}, [case], packet)


@pytest.mark.parametrize("defect", ["accept_false_origin", "accept_duplicate", "legacy_completeness", "false_accept_category", "false_reject_category", "bad_distinctness"])
def test_contradictory_axes_are_malformed_not_feedback(corpus: Corpus, defect: str) -> None:
    packet = corpus.packet(next(iter(corpus.by_id)))
    case = draft(packet, "inspect")
    verdict = decision(packet, 0, case)
    if defect == "accept_false_origin":
        verdict["origin_association"]["supported"] = False
    elif defect == "accept_duplicate":
        verdict["goal_distinctness"]["distinct"] = False
    elif defect == "legacy_completeness":
        verdict["target_completeness"] = {"complete": False}
    elif defect == "false_accept_category":
        verdict["accept"] = False
    elif defect == "false_reject_category":
        verdict.update(accept=False, category="duplicate_intent")
    else:
        verdict["goal_distinctness"]["distinct"] = "true"
    with pytest.raises(ValueError):
        validate_judgment({"decisions": [verdict]}, [case], packet)


def test_separate_role_seams_and_durable_role_request(corpus: Corpus, tmp_path: Path) -> None:
    generator, validator = SyntheticModel(), SyntheticModel()

    def generation(prompt: str) -> dict[str, Any]:
        assert prompt.startswith(GENERATION)
        return generator(prompt)

    def judgment(prompt: str) -> dict[str, Any]:
        assert prompt.startswith(JUDGMENT)
        return validator(prompt)

    campaign = Campaign(corpus, tmp_path / "campaign", generation, judgment)
    origin = next(iter(corpus.by_id))
    campaign.prepare([origin])
    assert len(generator.calls) == len(validator.calls) == 1
    directory = campaign.root / "artifacts" / origin / "round-0000"
    for role, model, effort in [("generation", "gpt-6-luna", "xhigh"), ("judgment", "gpt-6.1-sol", "high")]:
        record = read_json(directory / (role + ".json"))
        assert record["request"]["role"] == role
        assert record["request"]["model"] == model
        assert record["request"]["reasoning"] == effort
        assert record["request"]["provider"] == "openai-codex"
    record = read_json(directory / "judgment.json")
    record["request"]["role"] = "generation"
    private_write(directory / "judgment.json", record)
    with pytest.raises(PreparationBlocked, match="role drift"):
        campaign.freeze()


def test_negative_neighbor_judgment_retries_only_validator(corpus: Corpus, tmp_path: Path) -> None:
    generator, validator = SyntheticModel(), SyntheticModel()

    def malformed(prompt: str) -> dict[str, Any]:
        response = validator(prompt)
        verdict = json.loads(response["text"])
        verdict["decisions"][0].update(accept=False, category="origin_mismatch")
        verdict["decisions"][0]["origin_association"].update(supported=False, logical_id="neighbor")
        response["text"] = json.dumps(verdict)
        return response

    campaign = Campaign(corpus, tmp_path / "campaign", generator, malformed)
    origin = next(iter(corpus.by_id))
    with pytest.raises(PreparationBlocked, match="Invalid judgment"):
        campaign.prepare([origin], rounds=1)
    run = campaign.root / "artifacts" / origin / "round-0000"
    assert not (run / "result.json").exists()
    assert len(generator.calls) == len(validator.calls) == 1
    Campaign(corpus, campaign.root, generator, validator).prepare([origin])
    assert len(generator.calls) == 1 and len(validator.calls) == 2



def test_additions_materialize_without_repairing_raw_drafts_or_wrong_siblings(tmp_path: Path) -> None:
    c = contextual_corpus(tmp_path)
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "cron:widget-update-acceptance")
    generator, validator = SyntheticModel(), SyntheticModel()

    def judgment(prompt: str) -> dict[str, Any]:
        response = validator(prompt)
        if len(validator.calls) == 1:
            data = json.loads(prompt[len(JUDGMENT):])
            neighbor = data["packet"]["sources"][1]
            obj = json.loads(response["text"])
            constructive = obj["decisions"][0]
            constructive["additional_targets"] = [{
                "logical_id": neighbor["logical_id"], "evidence_id": neighbor["spans"][0]["evidence_id"],
                "quote": neighbor["spans"][0]["text"], "rationale": "Useful alternative fixture first read"}]
            incorrect = obj["decisions"][1]
            incorrect.update(accept=False, category="unsupported_target", reason="Not useful for this goal")
            incorrect["target_correctness"][0]["supported"] = False
            response["text"] = json.dumps(obj)
        return response

    campaign = Campaign(c, tmp_path / "campaign", generator, judgment)
    with pytest.raises(PreparationBlocked, match="quota unmet"):
        campaign.prepare([origin], rounds=1)
    accepted = campaign.accepted(origin)
    assert len(accepted) == 4
    run = campaign.root / "artifacts" / origin / "round-0000"
    result = read_json(run / "result.json")
    assert {r["category"] for r in result["rejected"]} == {"unsupported_target"}
    generation = json.loads(read_json(run / "generation.json")["response"]["text"])
    assert result["rejected"][0]["draft"] == generation["cases"][1]
    assert len(result["rejected"][0]["draft"]["targets"]) == 1  # No in-place omission repair.
    assert len(accepted[0]["targets"]) == 2
    assert len(generation["cases"][0]["targets"]) == 1
    assert len(campaign.verify_cases(origin)) == 4
    generation_bytes = (run / "generation.json").read_bytes()
    judgment_bytes = (run / "judgment.json").read_bytes()
    campaign.prepare([origin], rounds=1)
    assert (run / "generation.json").read_bytes() == generation_bytes
    assert (run / "judgment.json").read_bytes() == judgment_bytes
    assert campaign.accepted(origin)[:4] == accepted
    assert len(generator.calls) == len(validator.calls) == 2
    payload = json.loads(generator.calls[-1][len(GENERATION):])
    assert payload["missing"] == 1 and len(payload["accepted"]) == 4
    assert all("judgment" in r for r in payload["rejected"])
    assert len(campaign.verify_cases(origin)) == 5


@pytest.mark.parametrize("defect", ["extra_target", "missing_addition", "changed_citation", "changed_draft",
                                  "judgment_drafts", "judgment_context", "cross_round", "prompt_prefix"])
def test_materialized_readback_binds_exact_received_draft_and_judgment(tmp_path: Path, defect: str) -> None:
    c = contextual_corpus(tmp_path)
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "cron:widget-update-acceptance")
    model = SyntheticModel()

    def judge(prompt: str) -> dict[str, Any]:
        response = model(prompt)
        packet = json.loads(prompt[len(JUDGMENT):])["packet"]
        neighbor = packet["sources"][1]
        obj = json.loads(response["text"])
        obj["decisions"][0]["additional_targets"] = [{
            "logical_id": neighbor["logical_id"], "evidence_id": neighbor["spans"][0]["evidence_id"],
            "quote": neighbor["spans"][0]["text"], "rationale": "Useful update acceptance first read"}]
        response["text"] = json.dumps(obj)
        return response

    campaign = Campaign(c, tmp_path / "campaign", model, judge)
    campaign.prepare([origin])
    cases = campaign.verify_cases(origin)
    case = cases[0]
    assert len(case["targets"]) == 2
    run = campaign.root / "artifacts" / origin / "round-0000"
    generation = read_json(run / "generation.json")
    judgment = read_json(run / "judgment.json")
    raw = json.loads(generation["response"]["text"])["cases"][0]
    assert len(raw["targets"]) == 1
    assert case["case_id"] == digest([campaign.identity, origin, raw["question"], raw["intent"], case["targets"]])
    if defect in {"extra_target", "missing_addition", "changed_citation", "changed_draft"}:
        if defect == "extra_target":
            packet = c.packet(origin)
            source = packet["sources"][2]
            case["targets"].append({"logical_id": source["logical_id"], "evidence_id": source["spans"][0]["evidence_id"],
                                    "quote": source["spans"][0]["text"], "rationale": "Forged but literally cited label"})
        elif defect == "missing_addition":
            case["targets"].pop()
        elif defect == "changed_citation":
            case["targets"][-1]["rationale"] = "Changed materialization"
        else:
            case["question"] = "Forged recognizable question"
        # Recomputing the digest must not make an unauthorized transformation valid.
        case["case_id"] = digest([campaign.identity, origin, case["question"], case["intent"], case["targets"]])
    elif defect in {"judgment_drafts", "judgment_context", "prompt_prefix"}:
        request = json.loads(judgment["request"]["prompt"][len(JUDGMENT):])
        if defect == "judgment_drafts":
            request["drafts"][0]["question"] = "Changed question not received from generation"
        elif defect == "judgment_context":
            request["accepted"] = [raw]
        prefix = "X" * len(JUDGMENT) if defect == "prompt_prefix" else JUDGMENT
        judgment["request"]["prompt"] = prefix + encoded(request).decode()
        judgment["request"]["prompt_sha256"] = hashlib.sha256(judgment["request"]["prompt"].encode()).hexdigest()
        private_write(run / "judgment.json", judgment)
    else:
        other = run.with_name("round-0001") / "judgment.json"
        private_write(other, judgment)
        case["judgment_receipt"] = f"artifacts/{origin}/round-0001/judgment.json"
    private_write(run.parent / "accepted.json", cases)
    with pytest.raises(PreparationBlocked, match="binding drift|no longer matches"):
        campaign.verify_cases(origin)


def test_constructive_union_order_is_deterministic_and_does_not_mutate_draft(tmp_path: Path) -> None:
    from scripts.lab_benchmark import materialize_targets
    c = contextual_corpus(tmp_path)
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "cron:widget-update-acceptance")
    packet = c.packet(origin)
    case = draft(packet, "inspect")
    verdict = decision(packet, 0, case)
    verdict["additional_targets"] = [{"logical_id": s["logical_id"], "evidence_id": s["spans"][0]["evidence_id"],
                                      "quote": s["spans"][0]["text"], "rationale": "Useful acceptance source"}
                                     for s in packet["sources"][1:3]]
    validate_judgment({"decisions": [verdict]}, [case], packet)
    before = encoded(case)
    union = materialize_targets(case, verdict)
    verdict["additional_targets"].reverse()
    assert materialize_targets(case, verdict) == union
    assert union[:1] == case["targets"] and encoded(case) == before
    assert [t["logical_id"] for t in union[1:]] == sorted(t["logical_id"] for t in union[1:])


@pytest.mark.parametrize("intent", ["off-origin", "unrelated-addition", "fabricated-capability", "duplicate-goal"])
def test_semantic_rejections_do_not_materialize_additions(tmp_path: Path, intent: str) -> None:
    c = contextual_corpus(tmp_path)
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "cron:widget-update-acceptance")
    model = SyntheticModel()

    def judge(prompt: str) -> dict[str, Any]:
        response = model(prompt)
        packet = json.loads(prompt[len(JUDGMENT):])["packet"]
        source = packet["sources"][1]
        obj = json.loads(response["text"])
        d = obj["decisions"][0]
        d["additional_targets"] = [{"logical_id": source["logical_id"], "evidence_id": source["spans"][0]["evidence_id"],
                                    "quote": source["spans"][0]["text"], "rationale": "Literal quote alone is not semantic support"}]
        d.update(accept=False, category="unsupported_target", reason=intent)
        if intent == "off-origin":
            d["category"] = "origin_mismatch"
            d["origin_association"]["supported"] = False
        elif intent == "duplicate-goal":
            d["category"] = "duplicate_intent"
            d["goal_distinctness"]["distinct"] = False
        response["text"] = json.dumps(obj)
        return response

    campaign = Campaign(c, tmp_path / "campaign", model, judge)
    with pytest.raises(PreparationBlocked, match="quota unmet"):
        campaign.prepare([origin], rounds=1)
    assert len(campaign.verify_cases(origin)) == 4
    assert all(len(case["targets"]) == 1 for case in campaign.accepted(origin))
    result = read_json(campaign.root / "artifacts" / origin / "round-0000/result.json")
    assert len(result["rejected"]) == 1 and result["rejected"][0]["reason"] == intent


@pytest.mark.parametrize("budget", [1, 999, 4000, 24000, 24567])
def test_task_relevant_paragraph_selection_is_bounded_and_offset_exact(budget: int) -> None:
    text = "# Reference\n\n" + "Ordinary background reference paragraph.\n\n" * 3000 + "# End\n"
    baseline = select_spans(text)
    # Place invented task prose well inside a structural/even coverage gap.
    gap = next((left[1], right[0]) for left, right in zip(baseline, baseline[1:]) if right[0] - left[1] > 1500)
    start = gap[0] + 400
    paragraph = "\n\nAcceptance tracker: inspect widget migration status after activation.\n\n"
    text = text[:start] + paragraph + text[start + len(paragraph):]
    context = "Verify widget migration acceptance after activation"
    spans = select_spans(text, budget, context=context)
    assert sum(end - begin for begin, end in spans) <= budget
    assert all(0 <= begin < end <= len(text) for begin, end in spans)
    assert all(left[1] < right[0] for left, right in zip(spans, spans[1:]))
    assert spans == select_spans(text, budget, context=context)
    if budget >= 4000:
        selected = "\n".join(text[begin:end] for begin, end in spans)
        assert "Acceptance tracker: inspect widget migration status after activation." in selected
        assert spans[0][0] == 0 and spans[-1][1] == len(text)
    with pytest.raises(ValueError, match="Positive"):
        select_spans(text, 0)


def test_packet_selects_task_relevant_paragraphs_from_frozen_origin_context(tmp_path: Path) -> None:
    c = contextual_corpus(tmp_path)
    mapping = read_json(c.root / "mapping.json")
    report = next(a for a in mapping if a["id"] == "doc:widget-update-report")
    body = "# Reference\n\n" + "General unrelated historical background.\n\n" * 3000
    paragraph = "\n\nWidget update acceptance tracker. Check widget migration status after activation.\n\n"
    body = body[:13777] + paragraph + body[13777:]
    assert not any(paragraph.strip() in body[start:end] for start, end in select_spans(body))
    c.path(report).write_text(body)
    report["content_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    (c.root / "mapping.json").write_text(json.dumps(mapping))
    (c.root / "manifest.json").write_text(json.dumps({"inputs": frozen_manifest(c.root), "policy": "synthetic-fixture", "settings": {}}))
    c = Corpus(c.root, hashlib.sha256((c.root / "manifest.json").read_bytes()).hexdigest())
    origin = next(a["logical_id"] for a in c.artifacts if a["id"] == "cron:widget-update-acceptance")
    source = next(s for s in c.packet(origin)["sources"] if s["logical_id"] == report["logical_id"])
    assert any(paragraph.strip() in span["text"] for span in source["spans"])
    assert sum(len(span["text"]) for span in source["spans"]) <= 24000
    for span in source["spans"]:
        assert span["text"] == body[span["start"]:span["end"]]
        assert span["evidence_id"] == digest([source["logical_id"], span["start"], span["end"], span["text"]])


def test_v5_prompt_accepts_imperfect_goals_without_five_distinct_capabilities() -> None:
    from scripts.lab_benchmark import PROTOCOL
    assert PROTOCOL["name"] == "lk-synthetic-routing-v5"
    assert PROTOCOL["quota"] == 5
    for prompt in [GENERATION, JUDGMENT]:
        assert "Imperfect but" in prompt and "top five" in prompt
    assert "NOT necessarily distinct tool capabilities" in JUDGMENT
    assert "Missing alternatives\nare NOT a rejection reason" in JUDGMENT
    assert "target_completeness" not in JUDGMENT and "incomplete_targets" not in JUDGMENT


def test_sparse_single_capability_can_support_five_distinct_goals() -> None:
    text = "Inspect the current rendered screen visually. No interaction or historical capture is available."
    packet: dict[str, Any] = {"origin": "synthetic-screen", "sources": [{"logical_id": "synthetic-screen", "spans": [{
        "evidence_id": "synthetic-span", "text": text}]}]}
    goals = [
        ("The form looks clipped; where do I check what is actually visible?", "diagnose clipping"),
        ("Can I inspect whether the error message is visible before filing a report?", "document visible error"),
        ("Where can I check the layout after my stylesheet change?", "verify layout change"),
        ("I need to check if the present screen is suitable for a demonstration.", "prepare demonstration"),
        ("Where do I inspect the displayed page before deciding whether to continue?", "decide next manual step"),
    ]
    cases: list[dict[str, Any]] = [{"question": question, "intent": intent, "targets": [{"logical_id": "synthetic-screen",
              "evidence_id": "synthetic-span", "quote": "Inspect the current rendered screen visually.",
              "rationale": "Visual inspection is a useful first read for this goal"}]} for question, intent in goals]
    assert validate_drafts({"cases": cases}, packet, 5) == cases
    # Sender-seam decisions test the controller, not a claim about real model quality.
    decisions = [decision(packet, index, case) for index, case in enumerate(cases)]
    assert all(d["accept"] for d in validate_judgment({"decisions": decisions}, cases, packet))
    assert len({c["intent"] for c in cases}) == 5
    assert len({t["logical_id"] for c in cases for t in c["targets"]}) == 1
    invented = {**cases[0], "question": "Where do I inspect yesterday's screen recording?", "intent": "recover history"}
    rejected = decision(packet, 0, invented)
    rejected.update(accept=False, category="unsupported_target", reason="Historical capture is not supported")
    rejected["target_correctness"][0]["supported"] = False
    assert not validate_judgment({"decisions": [rejected]}, [invented], packet)[0]["accept"]
