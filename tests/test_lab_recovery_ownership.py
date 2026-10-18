"""Recovery owns only exact receipt-bound exports, never a conversation replay."""
import json
import os

import pytest

from scripts import lab_agent as agent
from scripts import lab_experiment as lab
from scripts import lab_recovery as recovery
from tests.test_lab_recovery import pending_candidate
from tests.test_lab_experiment import Harness


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def setup_receipt(attempt, relative="implementation", *, image=agent.IMAGE):
    root = attempt / relative
    children = ("home", "work", "scratch", "output") if relative.endswith(("implementation", "review")) else ("home", "work", "output")
    for child in children:
        (root / child).mkdir(parents=True, exist_ok=True)
    args = ["docker", "run", "--rm", "--name", "lk-lab-owned-setup", "--label",
            "lk.lab=ticket-69" if len(children) == 4 else "lk.lab=ticket-66",
            "--network", "none", "--user", "0:0", "--entrypoint", "/bin/chown"]
    for child in children:
        args += ["--mount", f"type=bind,src={root / child},dst=/{child}"]
    args += [image, "-R", "10000:10000", *["/" + c for c in children]]
    with recovery.resources(attempt):
        recovery.plan_resource(args, image)
    return root


@pytest.mark.parametrize("relative", ["implementation", "review", "generation-1/review",
                                       "evaluation-incumbent", "generation-2/evaluation-candidate"])
def test_incomplete_recovery_restores_known_exports_before_publish(h, monkeypatch, relative):
    attempt = pending_candidate(h)
    (attempt / "implementation-result.json").unlink()
    private = attempt / "implementation/private"
    private.mkdir()
    lab.durable_json(private / "session.json", {"role": "implementation", "session_id": "synthetic-partial", **lab.MODEL})
    lab.durable_json(private / "tool-intent-1-0.json", {})
    root = setup_receipt(attempt, relative)
    monkeypatch.setattr(recovery, "docker_inspect", lambda name: None)
    events = []
    monkeypatch.setattr(agent, "run", lambda args: events.append(args) or "")
    monkeypatch.setattr(agent, "quiesce_helper", lambda *args: None)
    publish = lab.publish
    def checked(*args, **kwargs):
        assert len(events) == 1, "abandoned exports must be restored before publication"
        assert f"{os.getuid()}:{os.getgid()}" in events[0]
        assert "--from=10000:10000" in events[0]
        assert all(str(root / c) in " ".join(events[0]) for c in ("home", "work", "output"))
        return publish(*args, **kwargs)
    monkeypatch.setattr(lab, "publish", checked)
    calls, evaluations = len(h.calls), len(h.evaluations)
    assert h.run(recover_only=True)["outcome"] == "interrupted"
    assert h.manifest()["pending"] is None
    assert (len(h.calls), len(h.evaluations)) == (calls, evaluations)


@pytest.mark.parametrize("boundary", ["run", "quiesce"])
def test_restore_failure_keeps_pending_and_explicit_retry_restores(h, monkeypatch, boundary):
    attempt = pending_candidate(h)
    setup_receipt(attempt)
    monkeypatch.setattr(recovery, "docker_inspect", lambda name: None)
    monkeypatch.setattr(agent, "quiesce_helper", lambda *args: None)
    monkeypatch.setattr(agent, "run", lambda args: "")
    def failed(*args):
        raise OSError("synthetic helper failure")
    monkeypatch.setattr(agent, "run" if boundary == "run" else "quiesce_helper", failed)
    with pytest.raises(ValueError, match="export_ownership_restore_failed"):
        h.run(recover_only=True)
    assert h.manifest()["pending"] == attempt.name and h.manifest()["paused"]
    monkeypatch.setattr(agent, "run", lambda args: "")
    monkeypatch.setattr(agent, "quiesce_helper", lambda *args: None)
    assert h.run(recover_only=True)["outcome"] == "accepted"


@pytest.mark.parametrize("unsafe", ["live", "wrong_image", "unknown_path", "symlink"])
def test_no_restore_when_resource_or_export_identity_uncertain(h, monkeypatch, unsafe):
    attempt = pending_candidate(h)
    root = setup_receipt(attempt, "implementation" if unsafe != "unknown_path" else "unowned")
    monkeypatch.setattr(agent, "run", lambda args: pytest.fail("must not restore"))
    monkeypatch.setattr(recovery, "docker_inspect", lambda name: None)
    receipt = next((attempt / "resources").glob("*.json"))
    if unsafe == "live":
        monkeypatch.setattr(recovery, "settle_resources", lambda p: (_ for _ in ()).throw(
            recovery.RecoveryPaused("original_worker_still_running")))
    elif unsafe == "wrong_image":
        value = json.loads(receipt.read_text())
        value["image"] = "sha256:" + "a" * 64
        lab.durable_json(receipt, value, replace=True)
    elif unsafe == "symlink":
        (root / "home").rmdir()
        (root / "home").symlink_to(h.root / "corpus", target_is_directory=True)
    with pytest.raises(ValueError):
        h.run(recover_only=True)
    assert h.manifest()["pending"] == attempt.name and h.manifest()["paused"]


@pytest.mark.parametrize("relative", ["generation-1/review", "generation-2/evaluation-candidate"])
def test_abandoned_verification_restored_before_successor(h, monkeypatch, relative):
    attempt = pending_candidate(h)
    setup_receipt(attempt, relative)
    monkeypatch.setattr(recovery, "docker_inspect", lambda name: None)
    events = []
    monkeypatch.setattr(agent, "run", lambda args: events.append("restore") or "")
    monkeypatch.setattr(agent, "quiesce_helper", lambda *args: None)
    evaluator = h.evaluator
    def successor(**kwargs):
        assert events == ["restore"]
        assert "generation-" in str(kwargs["attempt"])
        return evaluator(**kwargs)
    h.evaluator = successor
    assert h.run(recover_only=True)["outcome"] == "accepted"


def test_finalized_pending_restore_metadata_bound_before_settlement(h, monkeypatch):
    from scripts import lab_retention as retention
    attempt = pending_candidate(h)
    setup_receipt(attempt)
    monkeypatch.setattr(recovery, "docker_inspect", lambda name: None)
    monkeypatch.setattr(agent, "run", lambda args: "")
    monkeypatch.setattr(agent, "quiesce_helper", lambda *args: None)
    report = lab.read_json(attempt / "draft-report.json")
    report.update(outcome="error", reconciliation_required=True)
    lab.publish(h.campaign, h.manifest(), report, paused=True, hold_pending=True, recovery_pause=True)
    original = (attempt / "report.json").read_bytes()
    assert h.run(recover_only=True)["outcome"] == "error"
    assert (attempt / "report.json").read_bytes() == original
    receipt = lab.read_json(attempt / "recovery-report.json")
    assert receipt["evidence"] == retention.settlement_evidence(attempt)
    assert len(receipt["resources"]) == 2  # Original setup plus restore helper.
    assert all(row["state"] == "absent" for row in receipt["resources"])
    assert h.manifest()["reports"][0]["recovery_sha256"] == lab.file_sha(attempt / "recovery-report.json")
    cleaned = retention.cleanup_campaign(h.campaign, now=100)
    assert not cleaned["blocked"] and cleaned["cleaned"][0]["disposable_complete"]
