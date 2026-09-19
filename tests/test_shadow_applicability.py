"""Scripted end-to-end contract regressions, NOT evidence of model semantic efficacy."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_local_knowledge import index, shadow
from hermes_local_knowledge.config import Config, IndexSettings, VerifiedRoutingSettings
from hermes_local_knowledge.shadow_sources import Evidence, identity


ATLAS = "runbook:docs-atlas-inventory"
TRACKER = "runbook:docs-atlas-tracker"
WORKFLOW = "runbook:docs-maintenance-workflow"
BOREAL = "runbook:docs-boreal-inventory"
BACKUP = "runbook:docs-atlas-backup"
BASELINE = [WORKFLOW, ATLAS, TRACKER]
PARENT = "Repair the maintenance automation and deploy its changes after reviewing the current progress."
LOOKUP = {"intent": "Find Atlas host inventory and maintenance progress tracker", "target": "Atlas",
          "operation": "inventory", "context": "Assistant lookup target chosen before search; live status is not established."}


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    home, root = tmp_path / "profile", tmp_path / "corpus"
    home.mkdir()
    (root / "docs").mkdir(parents=True)
    docs = {
        "atlas-inventory": "# Atlas host inventory\nAtlas runs scheduler, tracker and audit services; these are inventory facts, not repair instructions.\n",
        "atlas-tracker": "# Atlas maintenance progress tracker\nAtlas scheduler migration is pending; tracker records blockers and completed work absent from the inventory.\n",
        "maintenance-workflow": "# Generic maintenance workflow\nInspect service state, prepare changes, test and ask for deployment approval. No host inventory or progress records here.\n",
        "boreal-inventory": "# Boreal host inventory\nBoreal runs the image builder, not Atlas scheduler or its maintenance tracker.\n",
        "atlas-backup": "# Atlas backup recovery procedure\nFor backup recovery use this source; inventory and tracker do not specify restore validation.\n",
    }
    for name, body in docs.items():
        (root / "docs" / (name + ".md")).write_text(body)
    cfg = Config(source_root=root, hermes_home=home, state_dir=home / "state",
                 index_settings=IndexSettings(), verified_routing=VerifiedRoutingSettings(mode="shadow"))
    index.build_index(root, cfg.state_dir, home, cfg.index_settings)
    return cfg


def records(cfg: Config) -> list[dict[str, Any]]:
    with closing(shadow._connect(cfg)) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM cases ORDER BY created,id")]


def observe(cfg: Config, *, lookup: Any = LOOKUP, request: str = PARENT,
            baseline: list[str] | None = None, query: str = "Atlas inventory tracker") -> dict[str, Any]:
    return shadow.observe(cfg, user_request=request, query=query, artifact_type="",
                          session_id="session", task_id="task", turn_id="turn",
                          baseline_ids=BASELINE if baseline is None else baseline, lookup=lookup)


def cite(source: dict[str, Any]) -> dict[str, Any]:
    return {**{key: source[key] for key in ("id", "locator", "sha256")}, "start_line": 2, "end_line": 2}


class ScriptedModel:
    """Fixed fixture oracle tests worker plumbing; deliberately no semantic model claim."""

    def __init__(self, *, omit_tracker: bool = False, wrong_parent: bool = False,
                 reject_reuse: bool = False, unknown: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.omit_tracker, self.wrong_parent = omit_tracker, wrong_parent
        self.reject_reuse, self.unknown = reject_reuse, unknown

    def complete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        packet = json.loads(kwargs["input"][0]["text"])
        assert shadow._CONTEXT in kwargs["instructions"]
        context = packet["lookup_context"]
        assert context["contract_version"] == 2
        assert context["lookup"]["provenance"] == "assistant_supplied_not_authority"
        assert context["lookup"]["timing"] == "search_arguments_pre_result"
        fields = context["lookup"]["fields"]
        desired = ([BOREAL] if fields.get("target") == "Boreal" else
                   [BACKUP] if fields.get("operation") == "backup recovery" else [ATLAS, TRACKER])
        route = [WORKFLOW] if self.wrong_parent else desired[:1] if self.omit_tracker else desired
        available = {source["id"] for source in packet["sources"]}
        stage = kwargs["purpose"].rsplit(".", 1)[1]
        review = []
        for artifact_id in packet["baseline_ids"]:
            disposition = "retained" if artifact_id in desired else "not_useful"
            if self.unknown and artifact_id == TRACKER:
                disposition = "unknown"
            review.append({"id": artifact_id, "disposition": disposition,
                           "covered_by": [artifact_id] if artifact_id in route else [],
                           "reason": "Direct host inventory/progress evidence." if artifact_id in desired
                           else "Metadata describes another host or operation, not this lookup."})
        if stage == "applicability":
            selected = next((c for c in packet["stored_routes"] if c["route_ids"] == desired), None)
            answer = ({"verdict": "unresolved"} if self.reject_reuse or selected is None else
                      {"verdict": "applicable", "case_id": selected["case_id"], "route_ids": desired,
                       "lookup_supported": True, "baseline_review": review,
                       "citations": [cite(s) for s in packet["sources"]]})
        elif stage == "verifier":
            answer = {"verdict": "verified", "route_ids": route, "near_miss_id": packet["near_miss_id"],
                      "task_supported": True, "lookup_supported": not self.wrong_parent,
                      "near_miss_rejected": True, "baseline_review": review,
                      "citations": [cite(s) for s in packet["sources"]]}
        elif not set(desired + route) <= available:
            answer = {"action": "read", "ids": [i for i in dict.fromkeys(desired + route) if i not in available]}
        else:
            answer = {"action": "propose", "route_ids": route,
                      "citations": [cite(s) for s in packet["sources"] if s["id"] in route]}
        return SimpleNamespace(parsed=answer, usage={"input_tokens": 10, "output_tokens": 10})


def run(cfg: Config, model: ScriptedModel) -> dict[str, Any]:
    shadow.finish_session(cfg, "session")
    return shadow.run_batch(cfg, llm=model)


def test_immediate_inventory_not_parent_and_same_packet_at_every_stage(cfg: Config) -> None:
    assert observe(cfg)["observation"] == "fallback"
    model = ScriptedModel()
    assert run(cfg, model)["verified"] == 1
    row = records(cfg)[0]
    result = json.loads(row["result"])
    assert result["route_ids"] == [ATLAS, TRACKER] and result["contract_version"] == 2
    assert row["calls"] == 3
    packets = [json.loads(call["input"][0]["text"]) for call in model.calls]
    expected = shadow._task_packet(row)
    assert all({key: packet[key] for key in expected} == expected for packet in packets)
    assert all(packet["user_request"] == PARENT for packet in packets)
    assert result["baseline_review"][0]["disposition"] == "not_useful"


@pytest.mark.parametrize("option,reason", [("wrong_parent", "verifier_rejected"),
                                           ("omit_tracker", "baseline_coverage_lost"),
                                           ("unknown", "baseline_coverage_unknown")])
def test_generic_parent_or_lost_tracker_cannot_be_verified(cfg: Config, option: str, reason: str) -> None:
    observe(cfg)
    assert run(cfg, ScriptedModel(**{option: True}))["unresolved"] == 1
    assert records(cfg)[0]["reason"] == reason


@pytest.mark.parametrize("intent,user_request", [
    ("Locate Atlas machine services and unfinished maintenance work", "Show the host evidence before planning maintenance."),
    ("Find Atlas inventory and progress sources for a list of five rather than two services", PARENT),
    ("Locate Atlas inventory and progress tracker", "Continue"),
])
def test_worker_paraphrase_quantity_and_terse_lookup_reuse(cfg: Config, intent: str, user_request: str) -> None:
    observe(cfg)
    assert run(cfg, ScriptedModel())["verified"] == 1
    previous = records(cfg)[0]
    assert observe(cfg, lookup={**LOOKUP, "intent": intent}, request=user_request)["observation"] == "fallback"
    # Capture itself cannot run inference or equate words with semantic applicability.
    assert records(cfg)[1]["calls"] == 0
    model = ScriptedModel()
    assert run(cfg, model) == {"claimed": 1, "verified": 0, "unresolved": 0, "would_reuse": 1}
    assert len(model.calls) == 1 and model.calls[0]["purpose"].endswith("applicability")
    row = records(cfg)[1]
    assert row["status"] == "would_reuse"
    assert row["verified_at"] == previous["verified_at"]  # No freshness extension or chaining.
    result = json.loads(row["result"])
    assert result["provenance"] == "ai_applicable" and result["matched_case_id"] == previous["id"]
    assert observe(cfg, lookup={**LOOKUP, "intent": intent}, request=user_request)["observation"] == "would_reuse"
    assert shadow.report(cfg)["counters"]["would_reuse_semantic"] == 1


@pytest.mark.parametrize("fields,desired", [({"target": "Boreal"}, [BOREAL]),
                                            ({"operation": "backup recovery"}, [BACKUP])])
def test_wrong_target_or_operation_falls_through_to_new_acquisition(
    cfg: Config, fields: dict[str, str], desired: list[str],
) -> None:
    observe(cfg)
    assert run(cfg, ScriptedModel())["verified"] == 1
    new_lookup = {**LOOKUP, **fields, "intent": "Find the requested host operation source"}
    assert observe(cfg, lookup=new_lookup, baseline=[*desired, *BASELINE])["observation"] == "fallback"
    model = ScriptedModel()
    assert run(cfg, model)["verified"] == 1
    assert [c["purpose"].rsplit(".", 1)[1] for c in model.calls] == [
        "applicability", "investigator", "investigator", "verifier"]
    result = json.loads(records(cfg)[1]["result"])
    assert result["route_ids"] == desired and result["provenance"] == "ai_verified"


def test_full_thirty_baseline_coverage_and_current_metadata_fingerprint(cfg: Config) -> None:
    extra = []
    for n in range(27):
        name = f"unrelated-{n}"
        (cfg.source_root / "docs" / f"{name}.md").write_text(f"# Gardening reference {n}\nPlant care unrelated to hosts.\n")
        extra.append("runbook:docs-" + name)
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    baseline = [WORKFLOW, ATLAS, *extra, TRACKER]
    observe(cfg, baseline=baseline)
    model = ScriptedModel()
    assert run(cfg, model)["verified"] == 1
    row = records(cfg)[0]
    assert json.loads(row["baseline_ids"]) == baseline
    assert len(json.loads(row["result"])["baseline_review"]) == 30
    assert all(json.loads(c["input"][0]["text"])["baseline_ids"] == baseline for c in model.calls)
    assert observe(cfg, baseline=baseline)["observation"] == "would_reuse"
    # Same ID/order but newly useful metadata must trigger a new coverage decision.
    with sqlite3.connect(cfg.state_dir / "index.sqlite") as conn:
        conn.execute("UPDATE artifacts SET summary='Atlas current service changes' WHERE id=?", (extra[-1],))
    assert observe(cfg, baseline=baseline)["observation"] == "fallback"


def test_rejected_full_source_candidate_does_not_spend_acquisition_evidence_budget(cfg: Config) -> None:
    # Populate eight real readable dependencies of an old route, exhausting applicability's allowance.
    for n in range(3):
        (cfg.source_root / "docs" / f"atlas-extra-{n}.md").write_text(f"# Atlas evidence {n}\nAtlas supplemental inventory history.\n")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    observe(cfg)
    assert run(cfg, ScriptedModel())["verified"] == 1
    row = records(cfg)[0]
    result = json.loads(row["result"])
    ids = [ATLAS, TRACKER, WORKFLOW, BACKUP, BOREAL, *[f"runbook:docs-atlas-extra-{n}" for n in range(3)]]
    evidence = Evidence(cfg, "")
    evidence.include(ids)
    result["sources"] = [identity(evidence.read(i)) for i in ids]
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        conn.execute("UPDATE cases SET result=? WHERE id=?", (json.dumps(result), row["id"]))
    # A ninth, new source is not among those receipts.
    (cfg.source_root / "docs" / "boreal-inventory.md").write_text("# Boreal host inventory\nBoreal new inventory evidence.\n")
    # Keep old eight current by replace BOREAL dependency with another private synthetic source.
    (cfg.source_root / "docs" / "atlas-extra-3.md").write_text("# Atlas historical service\nOld Atlas inventory evidence.\n")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    evidence = Evidence(cfg, "")
    ids[4] = "runbook:docs-atlas-extra-3"
    evidence.include(ids)
    result["sources"] = [identity(evidence.read(i)) for i in ids]
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        conn.execute("UPDATE cases SET result=? WHERE id=?", (json.dumps(result), row["id"]))
    observe(cfg, lookup={**LOOKUP, "target": "Boreal", "intent": "Find Boreal host inventory"}, baseline=[BOREAL, WORKFLOW])
    model = ScriptedModel()
    assert run(cfg, model)["verified"] == 1
    assert len(model.calls) == 4
    acquisition = json.loads(model.calls[1]["input"][0]["text"])
    assert acquisition["sources"] == [] and acquisition["read_refusals"] == {}
    assert json.loads(records(cfg)[1]["result"])["route_ids"] == [BOREAL]


def test_legacy_gate_and_additive_column_migration(cfg: Config) -> None:
    observe(cfg)
    assert run(cfg, ScriptedModel())["verified"] == 1
    row = records(cfg)[0]
    result = json.loads(row["result"])
    result.pop("contract_version")
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        conn.execute("UPDATE cases SET result=?", (json.dumps(result),))
    assert observe(cfg)["observation"] == "fallback"
    model = ScriptedModel()
    assert run(cfg, model)["verified"] == 1
    assert not any(c["purpose"].endswith("applicability") for c in model.calls)
    # Simulate the previous schema without deleting other queue/state data.
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        conn.execute("ALTER TABLE cases DROP COLUMN lookup_context")
        conn.execute("UPDATE cases SET status='pending',ready=1")
    assert shadow.report(cfg)["cases"] == {"pending": 1}  # read-only does not migrate
    with closing(shadow._connect(cfg, create=True)) as conn:
        assert conn.execute("SELECT lookup_context FROM cases").fetchone()[0] == "{}"
    model = ScriptedModel()
    assert run(cfg, model)["unresolved"] == 1 and model.calls == []
    assert records(cfg)[0]["reason"] == "legacy_context"


def test_shortlist_is_bounded_and_single_applicability_call_before_acquisition(cfg: Config) -> None:
    for n in range(4):
        observe(cfg, lookup={**LOOKUP, "intent": f"Find Atlas inventory and progress for review {n}"})
        assert run(cfg, ScriptedModel(reject_reuse=True))["verified"] == 1
    observe(cfg, lookup={**LOOKUP, "intent": "Locate the Atlas inventory and tracker sources"})
    model = ScriptedModel()
    assert run(cfg, model)["would_reuse"] == 1
    assert len(model.calls) == 1
    packet = json.loads(model.calls[0]["input"][0]["text"])
    assert len(packet["stored_routes"]) == shadow.MAX_REUSE_CANDIDATES == 3
    assert len(packet["sources"]) <= 8


def test_applicability_claim_without_tracker_coverage_falls_through(cfg: Config) -> None:
    observe(cfg)
    assert run(cfg, ScriptedModel())["verified"] == 1
    observe(cfg, lookup={**LOOKUP, "intent": "Find Atlas host inventory and maintenance state"})

    class LostCoverage(ScriptedModel):
        def complete_structured(self, **kwargs: Any) -> Any:
            response = super().complete_structured(**kwargs)
            if kwargs["purpose"].endswith("applicability"):
                # Even an affirmative applicability verdict cannot omit the tracker's review.
                response.parsed["baseline_review"] = [item for item in response.parsed["baseline_review"]
                                                     if item["id"] != TRACKER]
            return response

    model = LostCoverage()
    assert run(cfg, model)["verified"] == 1
    assert len(model.calls) == 4
    assert records(cfg)[1]["status"] == "ai_verified"
    assert "would_reuse_semantic" not in shadow.report(cfg)["counters"]


@pytest.mark.parametrize("lookup", [{"context": "x" * 1001}, {"intent": 42}, {"authority": "user"},
                                     {"provenance": "authoritative user fact"}, "not an object"])
def test_lookup_bounds_reject_instead_of_truncation_or_authority_upgrade(cfg: Config, lookup: Any) -> None:
    result = observe(cfg, lookup=lookup)
    assert result["status"] == "skipped"
    assert records(cfg) == []


def test_off_profile_stale_and_unchanged_total_call_budget(cfg: Config) -> None:
    disabled = replace(cfg, verified_routing=replace(cfg.verified_routing, mode="off"))
    assert observe(disabled)["status"] == "off" and not shadow._path(cfg).exists()
    observe(cfg)
    assert run(cfg, ScriptedModel())["verified"] == 1
    other = replace(cfg, hermes_home=cfg.hermes_home / "other")
    observe(other, lookup={**LOOKUP, "intent": "Locate Atlas inventory sources"})
    model = ScriptedModel()
    assert run(other, model)["verified"] == 1
    assert all(not c["purpose"].endswith("applicability") for c in model.calls)
    with closing(shadow._connect(cfg, create=True)) as conn, conn:
        conn.execute("UPDATE cases SET verified_at=1")
    observe(cfg, lookup={**LOOKUP, "intent": "Find the Atlas host inventory documents"})
    model = ScriptedModel()
    assert run(cfg, model)["verified"] == 1
    assert all(not c["purpose"].endswith("applicability") for c in model.calls)
    bounded = replace(cfg, verified_routing=replace(cfg.verified_routing, max_model_calls_per_case=3))
    observe(bounded, lookup={**LOOKUP, "intent": "Find Atlas inventory records again"})
    model = ScriptedModel(reject_reuse=True)
    assert run(bounded, model)["unresolved"] == 1
    assert len(model.calls) == 2 and records(cfg)[-1]["reason"] == "call_budget"


def test_search_history_preserves_attempts_and_existing_budget(cfg: Config) -> None:
    evidence = Evidence(cfg, "")
    for _ in range(6):
        evidence.search("Atlas inventory")
    history = evidence.packet()["search_history"]
    assert len(history) == 6
    assert history[0]["new_candidate_ids"]
    assert all(item == {"query": "Atlas inventory", "new_candidate_ids": []} for item in history[1:])
    with pytest.raises(ValueError, match="search_budget"):
        evidence.search("another query")
    assert len(history) == 6


def test_worker_exposes_unproductive_search_to_next_investigator_call(cfg: Config) -> None:
    class SearchThenAbstain(ScriptedModel):
        def complete_structured(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            packet = json.loads(kwargs["input"][0]["text"])
            history = packet["search_history"]
            assert "Do not repeat tried" in kwargs["instructions"]
            if len(self.calls) == 1:
                assert [item["query"] for item in history] == ["Atlas inventory tracker", LOOKUP["intent"]]
                answer = {"action": "search", "query": "nonexistent-procedure-zephyr"}
            else:
                assert len(history) == 3
                assert history[-1] == {"query": "nonexistent-procedure-zephyr", "new_candidate_ids": []}
                answer = {"action": "unresolved"}
            return SimpleNamespace(parsed=answer, usage={"input_tokens": 10, "output_tokens": 10})

    observe(cfg)
    model = SearchThenAbstain()
    assert run(cfg, model)["unresolved"] == 1
    assert len(model.calls) == 2
    assert records(cfg)[0]["reason"] == "insufficient_evidence"
