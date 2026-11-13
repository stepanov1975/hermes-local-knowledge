"""Initialization and prelaunch recovery with synthetic, nondispatched inputs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import lab_agent as agent, lab_experiment as lab, lab_recovery as recovery
from scripts import lab_retention as retention
from tests.test_lab_experiment import Harness


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


@pytest.mark.parametrize("relationship", ["inside", "equal", "ancestor", "symlink"])
def test_campaign_tool_overlap_rejected_before_any_write(h: Harness, relationship: str) -> None:
    tools = Path(h.config["check_tools"])
    target = tools / "nested-campaign"
    if relationship == "equal":
        target = tools
    elif relationship == "ancestor":
        target = tools.parent
    elif relationship == "symlink":
        alias = h.root / "tool-alias"
        alias.symlink_to(tools, target_is_directory=True)
        target = alias / "nested-campaign"
    before = {p.relative_to(h.root): p.read_bytes() for p in h.root.rglob("*") if p.is_file()}
    for _ in range(2):
        with pytest.raises(ValueError, match="Campaign and check-tools must not overlap"):
            lab.init_campaign(target, h.config, loader=h.loader)
    assert {p.relative_to(h.root): p.read_bytes() for p in h.root.rglob("*") if p.is_file()} == before
    assert not (tools / "nested-campaign").exists()


@pytest.mark.parametrize("name", ["examples/ordinary\nname.yaml", 'examples/tab\tquote"slash\\.yaml'])
def test_candidate_literal_filename_roundtrip_add_delete(h: Harness, name: str) -> None:
    workspace = h.root / "literal-export"
    h.export(h.campaign / "repo.git", h.initial, workspace)
    path = workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ordinary: true\n")
    add = h.root / "literal-add"
    add.mkdir()
    revision, changes = lab.candidate_commit(h.campaign / "repo.git", h.initial, workspace, add)
    assert changes == [{"path": name, "operation": "add"}]
    assert name in lab.tracked(h.campaign / "repo.git", revision)
    path.unlink()
    delete = h.root / "literal-delete"
    delete.mkdir()
    final, changes = lab.candidate_commit(h.campaign / "repo.git", revision, workspace, delete)
    assert changes == [{"path": name, "operation": "delete"}]
    assert lab.tracked(h.campaign / "repo.git", final) == lab.tracked(h.campaign / "repo.git", h.initial)


def fail_before_launch(h: Harness, monkeypatch: pytest.MonkeyPatch) -> Path:
    corpus = h.root / "corpus,separator"
    corpus.mkdir()
    config = lab.read_json(h.campaign / "config.json")
    config["corpus"] = str(corpus)
    lab.durable_json(h.campaign / "config.json", config, replace=True)
    lab.durable_json(h.campaign / "manifest.json", {
        **h.manifest(), "config_sha256": lab.sha(config)}, replace=True)
    original = agent.run
    calls = []

    def image_only(args: list[str], **kwargs: Any) -> str:
        if args[0] == "git":
            return original(args, **kwargs)
        calls.append(args)
        assert args[:3] == ["docker", "image", "inspect"], "must not dispatch resource or model"
        return json.dumps([{"Id": agent.IMAGE, "Config": {"Labels": {
            "org.opencontainers.image.revision": agent.SOURCE}}}])

    monkeypatch.setattr(agent, "run", image_only)
    monkeypatch.setattr(agent.model, "send", lambda *a, **kw: pytest.fail("must not call provider"))
    with pytest.raises(ValueError):
        lab.run_experiment(h.campaign, loader=h.loader)
    assert len(calls) == 1
    attempt = h.campaign / "attempts" / h.manifest()["pending"]
    assert recovery.inspect_resources(attempt) == []
    assert recovery.prelaunch_failure(attempt)
    assert not (attempt / "implementation/private/status.json").exists()
    return attempt


def test_finalized_prelaunch_failure_reconciles_without_relaunch(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    attempt = fail_before_launch(h, monkeypatch)
    report = (attempt / "report.json").read_bytes()
    recovered = lab.run_experiment(h.campaign, loader=h.loader, recover_only=True)
    assert recovered["outcome"] == "error"
    assert h.manifest()["pending"] is None and not h.manifest()["paused"]
    assert h.manifest()["incumbent"] == h.initial
    assert (attempt / "report.json").read_bytes() == report
    receipt = lab.read_json(attempt / "recovery-report.json")
    assert receipt["resources"] == []
    assert "implementation/private/prelaunch-failure.json" in receipt["evidence"]
    assert receipt["evidence"] == retention.settlement_evidence(attempt)
    assert lab.run_experiment(h.campaign, loader=h.loader, recover_only=True)["pending"] is None
    cleaned = retention.cleanup_campaign(h.campaign, now=0)
    assert cleaned["blocked"] == [] and len(cleaned["cleaned"]) == 1


@pytest.mark.parametrize("fault", ["missing_marker", "session_mismatch", "wrong_revision", "request", "wire", "resource_observed", "other_session"])
def test_empty_resources_do_not_authorize_uncertain_finalized_recovery(
        h: Harness, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    attempt = fail_before_launch(h, monkeypatch)
    private = attempt / "implementation/private"
    if fault == "missing_marker":
        (private / "prelaunch-failure.json").unlink()
    elif fault in {"session_mismatch", "wrong_revision"}:
        session = lab.read_json(private / "session.json")
        session["session_id" if fault == "session_mismatch" else "revision"] = "different"
        lab.durable_json(private / "session.json", session, replace=True)
        if fault == "wrong_revision":
            receipt = lab.read_json(private / "prelaunch-failure.json")
            receipt["session"] = session
            lab.durable_json(private / "prelaunch-failure.json", receipt, replace=True)
    elif fault in {"request", "wire"}:
        lab.durable_json(private / (fault + "-1.json"), {})
    elif fault == "resource_observed":
        (attempt / "resources").mkdir()
        lab.durable_json(attempt / "resources/lost.observed", {"container_id": "unknown"})
    else:
        (attempt / "review/private").mkdir(parents=True)
    assert not recovery.prelaunch_failure(attempt)
    pending = h.manifest()["pending"]
    for _ in range(2):
        with pytest.raises(ValueError, match="Campaign paused or pending"):
            lab.run_experiment(h.campaign, loader=h.loader, recover_only=True)
        assert h.manifest()["pending"] == pending and h.manifest()["paused"]
