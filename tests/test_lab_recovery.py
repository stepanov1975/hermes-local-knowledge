"""Disposable synthetic recovery: no real Docker or inference."""
import copy
import json
import subprocess
import sys

import pytest

from scripts import lab_experiment as lab
from scripts import lab_recovery as recovery
from tests.test_lab_experiment import Harness


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def pending_candidate(h):
    def interrupted(result):
        raise KeyboardInterrupt
    h.evaluation_mutate = interrupted
    with pytest.raises(KeyboardInterrupt):
        h.run()
    h.evaluation_mutate = None
    return h.campaign / "attempts" / h.manifest()["pending"]


@pytest.mark.parametrize("boundary", ["implementation", "evaluation", "checks", "review", "publication"])
def test_same_attempt_rechecks_after_interruption(h, monkeypatch, boundary):
    original_session, original_evaluator, original_publish = h.session, h.evaluator, lab.publish
    fired = False
    def session(**kwargs):
        nonlocal fired
        if boundary == "review" and kwargs["role"] == "review" and not fired:
            fired = True
            raise KeyboardInterrupt
        return original_session(**kwargs)
    def evaluator(**kwargs):
        nonlocal fired
        if boundary in {"implementation", "evaluation"} and not fired:
            fired = True
            raise KeyboardInterrupt
        return original_evaluator(**kwargs)
    def checks(**kwargs):
        nonlocal fired
        if boundary == "checks" and not fired:
            fired = True
            raise KeyboardInterrupt
        return h.checks(**kwargs)
    def publish(*args, **kwargs):
        nonlocal fired
        if boundary == "publication" and not fired:
            fired = True
            raise KeyboardInterrupt
        return original_publish(*args, **kwargs)
    monkeypatch.setattr(lab, "publish", publish)
    with pytest.raises(KeyboardInterrupt):
        lab.run_experiment(h.campaign, session=session, evaluator=evaluator, checks=checks, loader=h.loader)
    pending = h.manifest()["pending"]
    report = h.run()
    assert report["attempt_id"] == pending and report["outcome"] == "accepted"
    assert [c["role"] for c in h.calls].count("implementation") == 1
    assert len(h.manifest()["reports"]) == 1
    assert len(h.evaluations) >= 2
    assert any("generation-1" in str(e["attempt"]) for e in h.evaluations)
    assert report["unknown_target_gate"]["binding"]["verification_generation"] == 1


def resource(attempt, *, name="lk-lab-owned", label="ticket-69"):
    args = ["docker", "create", "--name", name, "--label", "lk.lab=" + label,
            "--mount", "type=bind,src=" + str(attempt) + ",dst=/candidate,readonly",
            "--entrypoint", "/bin/sleep", "sha256:" + "a" * 64, "infinity"]
    with recovery.resources(attempt):
        receipt = recovery.plan_resource(args, args[-2])
    spec = {"Id": "owned-id", "Name": "/" + name, "Image": args[-2],
            "Config": {"Labels": {"lk.lab": label}, "Entrypoint": ["/bin/sleep"], "Cmd": ["infinity"]},
            "Mounts": [{"Type": "bind", "Source": str(attempt.resolve()), "Destination": "/candidate", "RW": False}],
            "State": {"Running": False}}
    return receipt, spec


@pytest.mark.parametrize("label", ["ticket-69", "ticket-66"])
def test_live_original_is_preserved_then_explicit_recover_same_attempt(h, monkeypatch, label):
    attempt = pending_candidate(h)
    _, spec = resource(attempt, label=label)
    spec["State"]["Running"] = True  # sleep infinity is busy, not a completed exec.
    monkeypatch.setattr(recovery, "docker_inspect", lambda name: copy.deepcopy(spec))
    calls = len(h.calls)
    real_run = subprocess.run
    def guarded(args, **kwargs):
        assert args[0] != "docker", "must not remove live original"
        return real_run(args, **kwargs)
    monkeypatch.setattr(recovery.subprocess, "run", guarded)
    with pytest.raises(ValueError, match="original_worker_still_running"):
        h.run()
    assert len(h.calls) == calls and h.manifest()["paused"]
    assert lab.recovery_status(h.campaign)["recovery"]["reason"] == "original_worker_still_running"
    monkeypatch.setattr(recovery, "docker_inspect", lambda name: None)
    report = h.run(recover_only=True)
    assert report["attempt_id"] == attempt.name and report["outcome"] == "accepted"
    assert not h.manifest()["paused"]


@pytest.mark.parametrize("mutation", ["Id", "Image", "Name", "label", "mount", "command"])
def test_identity_mismatch_never_cleans_or_relaunches(h, monkeypatch, mutation):
    attempt = pending_candidate(h)
    receipt, spec = resource(attempt)
    recovery.observed_resource(receipt, "owned-id")
    if mutation in {"Id", "Image", "Name"}:
        spec[mutation] = "replacement"
    elif mutation == "label":
        spec["Config"]["Labels"]["lk.lab"] = "foreign"
    elif mutation == "mount":
        spec["Mounts"][0]["RW"] = True
    else:
        spec["Config"]["Cmd"] = ["other"]
    monkeypatch.setattr(recovery, "docker_inspect", lambda name: spec)
    calls = len(h.calls)
    with pytest.raises(ValueError, match="resource_identity_mismatch"):
        h.run()
    assert len(h.calls) == calls and h.manifest()["pending"] == attempt.name
    assert h.manifest()["incumbent"] == h.initial


def test_stopped_create_before_id_receipt_is_inspected_and_removed_not_forced(h, monkeypatch):
    attempt = pending_candidate(h)
    _, spec = resource(attempt)
    removed = []
    monkeypatch.setattr(recovery, "docker_inspect", lambda name: None if removed else spec)
    def remove(args, **kwargs):
        removed.append(args)
        return subprocess.CompletedProcess(args, 0)
    # Restore the actual subprocess boundary before Git and subsequent fixture exports.
    real_run = subprocess.run
    def run(args, **kwargs):
        return remove(args, **kwargs) if args[0] == "docker" else real_run(args, **kwargs)
    monkeypatch.setattr(recovery.subprocess, "run", run)
    report = h.run()
    assert report["attempt_id"] == attempt.name and report["outcome"] == "accepted"
    assert removed == [["docker", "rm", "--volumes", "owned-id"]]


def test_unavailable_docker_is_not_absence(tmp_path, monkeypatch):
    resource(tmp_path)
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr=b"private daemon detail")
    monkeypatch.setattr(recovery.subprocess, "run", unavailable)
    with pytest.raises(recovery.RecoveryPaused, match="resource_inspection_unavailable"):
        recovery.settle_resources(tmp_path)


@pytest.mark.parametrize("role", ["implementation", "review", "generation-1/review"])
@pytest.mark.parametrize("receipt", ["request", "wire"])
def test_unknown_model_completion_pauses_never_replays(h, role, receipt):
    attempt = pending_candidate(h)
    private = attempt / role / "private"
    private.mkdir(parents=True, exist_ok=True)
    lab.durable_json(private / (receipt + "-1.json"), {"synthetic": True})
    calls, evaluations = len(h.calls), len(h.evaluations)
    with pytest.raises(ValueError, match="model_completion_unknown"):
        h.run()
    for _ in range(2):
        with pytest.raises(ValueError, match="model_completion_unknown"):
            h.run(recover_only=True)
    assert len(h.calls) == calls and len(h.evaluations) == evaluations
    assert h.manifest()["paused"] and h.manifest()["incumbent"] == h.initial


def test_unknown_tool_never_replayed_and_settled_partial_finalizes_interrupted(h):
    attempt = pending_candidate(h)
    (attempt / "implementation-result.json").unlink()
    private = attempt / "implementation/private"
    private.mkdir()
    lab.durable_json(private / "session.json", {"role": "implementation", "session_id": "paid-session", **lab.MODEL})
    lab.durable_json(private / "tool-intent-1-0.json", {"command": "not replayable"})
    calls = len(h.calls)
    report = h.run()
    assert report["outcome"] == "interrupted" and report["attempt_id"] == attempt.name
    assert len(h.calls) == calls and h.manifest()["pending"] is None
    assert h.manifest()["incumbent"] == h.initial
    assert report["usage"][0]["usage_status"] == "unknown"
    next_report = h.run()
    assert next_report["attempt_id"] != attempt.name and len(h.manifest()["reports"]) == 2


def test_final_response_can_be_recovered_only_after_cleanup_and_source_validation(h, monkeypatch):
    from scripts import lab_agent as agent
    attempt = pending_candidate(h)
    result = lab.read_json(attempt / "implementation-result.json")
    (attempt / "implementation-result.json").unlink()
    workspace = attempt / "implementation/work/plugin"
    (workspace / "scripts/controller.py").unlink()  # Authentic narrow agent export omits controllers.
    private = attempt / "implementation/private"
    private.mkdir()
    metadata = {k: result[k] for k in ("session_id", "role", "revision", "model", "provider", "effort")}
    lab.durable_json(private / "session.json", {**metadata, "source_identity": agent.identity(workspace)})
    lab.durable_json(private / "request-1.json", {"synthetic": True})
    lab.durable_json(private / "response-1.json", {"message": {"content": json.dumps(result["final"])},
                                                "usage": {"input_tokens": 19}})
    events = []
    monkeypatch.setattr(agent, "ownership", lambda *args: events.append("ownership-restored"))
    report = h.run()
    assert events == ["ownership-restored"] and report["outcome"] == "accepted"
    assert report["usage"][0]["usage"] == [{"input_tokens": 19}]
    assert [c["role"] for c in h.calls].count("implementation") == 1


@pytest.mark.parametrize("drift", ["controller", "config", "candidate", "source", "benchmark"])
def test_recovery_drift_pauses_before_new_execution(h, drift):
    attempt = pending_candidate(h)
    if drift == "controller":
        h.identity["synthetic"] = "changed"
    elif drift == "config":
        cfg = lab.read_json(h.campaign / "config.json")
        cfg["composite_id"] = "changed"
        lab.durable_json(h.campaign / "config.json", cfg, replace=True)
    elif drift == "candidate":
        cfg = lab.read_json(attempt / "candidate.json")
        cfg["revision"] = "f" * 40
        lab.durable_json(attempt / "candidate.json", cfg, replace=True)
    elif drift == "source":
        (attempt / "implementation/work/plugin/hermes_local_knowledge/rank.py").write_text("AFTER = 1\n")
    else:
        def changed(**kwargs):
            raise ValueError("private benchmark drift detail")
        h.loader = changed
    calls, evaluations = len(h.calls), len(h.evaluations)
    with pytest.raises(ValueError):
        h.run()
    assert h.manifest()["paused"] and h.manifest()["pending"] == attempt.name
    assert len(h.calls) == calls and len(h.evaluations) == evaluations
    assert "private benchmark" not in json.dumps(lab.recovery_status(h.campaign))


def test_real_session_dispatch_paths_persist_intent_before_side_effects(tmp_path, monkeypatch):
    from scripts import lab_agent as agent, lab_agent_model as model
    from tests.test_lab_agent import FINAL, setup_session
    kwargs, _ = setup_session(tmp_path, monkeypatch)
    run = agent.run
    events = []
    def dispatch(args, **options):
        plans = list((tmp_path / "resources").glob("*.json"))
        if args[:2] == ["docker", "create"]:
            assert any(lab.read_json(p)["args"] == args for p in plans)
            events.append("create-intent")
        if args[:2] == ["docker", "start"]:
            assert any(p.with_suffix(".observed").exists() for p in plans)
            events.append("observed-before-start")
        return run(args, **options)
    monkeypatch.setattr(agent, "run", dispatch)
    responses = [
        {"content": None, "tool_calls": [{"id": "one", "type": "function", "function": {
            "name": "sandbox", "arguments": '{"command":"synthetic-command"}'}}]},
        {"content": json.dumps(FINAL), "tool_calls": []}]
    def send(messages, path):
        turn = 3 - len(responses)
        assert (path.parent / f"request-{turn}.json").exists()
        return {"message": responses.pop(0), "usage": {"available": False}}
    def sandbox(name, command, raw, container_id):
        assert (raw.parent / "tool-intent-1-0.json").exists()
        events.append("tool-intent")
        model.save(raw, {"synthetic": True})
        return "synthetic-result"
    monkeypatch.setattr(model, "send", send)
    monkeypatch.setattr(agent, "sandbox", sandbox)
    with recovery.resources(tmp_path):
        result = agent.run_session(**kwargs)
    assert result["complete"] and events == ["create-intent", "observed-before-start", "tool-intent"]
    assert recovery.session_completion(tmp_path) == "settled"


def test_cross_process_campaign_lock(h):
    code = "from pathlib import Path; from scripts.lab_experiment import run_experiment; " \
           "run_experiment(Path(__import__('sys').argv[1]))"
    with lab.campaign_lock(h.campaign):
        result = subprocess.run([sys.executable, "-c", code, str(h.campaign)], capture_output=True)
    assert result.returncode != 0 and b"BlockingIOError" in result.stderr


def test_launch_client_inherits_lock_even_if_controller_scope_exits(h):
    # Actual child process holds the inherited open-file-description flock. No PID reuse/TTL.
    with lab.campaign_lock(h.campaign):
        child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready',flush=True); sys.stdin.read()"],
                                 pass_fds=recovery.lock_fds(), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert child.stdout is not None
        assert child.stdout.readline() == b"ready\n"
    try:
        with pytest.raises(BlockingIOError):
            h.run()
        assert h.calls == []
    finally:
        child.communicate(b"", timeout=10)
    with lab.campaign_lock(h.campaign):
        pass


def test_atomic_receipts_never_expose_partial_final(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    def fail(*args):
        raise OSError("synthetic fsync failure")
    monkeypatch.setattr(recovery.os, "fsync", fail)
    with pytest.raises(OSError):
        recovery.atomic_json(path, {"complete": True})
    assert not path.exists()


@pytest.mark.parametrize("pending", [False, True])
def test_status_is_read_only(h, monkeypatch, pending):
    if pending:
        pending_candidate(h)
    monkeypatch.setattr(recovery, "docker_inspect", lambda *a: pytest.fail("status must not inspect Docker"))
    before = {str(p): p.read_bytes() for p in h.campaign.rglob("*") if p.is_file()}
    status = lab.recovery_status(h.campaign)
    assert bool(status["pending"]) is pending
    assert before == {str(p): p.read_bytes() for p in h.campaign.rglob("*") if p.is_file()}


def test_disk_pause_during_recovery_cannot_be_relabelled_or_cleared(h, monkeypatch):
    from scripts import lab_disk_guard as disk
    attempt = pending_candidate(h)
    calls = len(h.calls)
    low = [False]
    monkeypatch.setattr(disk, "available_space", lambda path: {
        "available_blocks": 9 if low[0] else 20, "usable_blocks": 100})
    def settle(path):
        assert path == attempt
        low[0] = True
        guard = disk._CURRENT.get()
        assert guard is not None
        guard.sample()
        return []
    monkeypatch.setattr(recovery, "settle_resources", settle)
    with pytest.raises(disk.LabDiskPaused):
        h.run()
    assert h.manifest()["paused"] and h.manifest()["pause_source"] == "disk"
    with pytest.raises(disk.LabDiskPaused):
        h.run(recover_only=True)
    assert h.manifest()["pending"] == attempt.name and len(h.calls) == calls
    assert h.manifest()["reports"] == []


def test_recover_without_pending_does_not_start_an_attempt(h):
    manifest = h.manifest()
    result = h.run(recover_only=True)
    assert result["pending"] is None and h.manifest() == manifest
    assert not h.calls and not list((h.campaign / "attempts").iterdir())


def test_manifest_already_published_is_not_republished_or_reverified(h, monkeypatch):
    publish = lab.publish
    def after_publication(*args, **kwargs):
        publish(*args, **kwargs)
        raise KeyboardInterrupt
    monkeypatch.setattr(lab, "publish", after_publication)
    with pytest.raises(KeyboardInterrupt):
        h.run()
    manifest, calls, evaluations = h.manifest(), len(h.calls), len(h.evaluations)
    assert manifest["pending"] is None and manifest["incumbent"] != h.initial
    for _ in range(2):
        h.run(recover_only=True)
    assert h.manifest() == manifest and len(h.calls) == calls and len(h.evaluations) == evaluations
    assert len(lab.finalized_reports(h.campaign, manifest)) == 1


def test_orphan_accepted_report_cannot_promote_when_refreshed_gate_fails(h, monkeypatch):
    durable = lab.durable_json
    def interrupt(path, value, *, replace=False):
        if path.name == "manifest.json" and value.get("reports"):
            raise KeyboardInterrupt
        durable(path, value, replace=replace)
    monkeypatch.setattr(lab, "durable_json", interrupt)
    with pytest.raises(KeyboardInterrupt):
        h.run()
    attempt = h.campaign / "attempts" / h.manifest()["pending"]
    assert lab.read_json(attempt / "report.json")["outcome"] == "accepted"
    monkeypatch.setattr(lab, "durable_json", durable)
    h.checks_passed = False
    report = h.run()
    assert report["outcome"] == "rejected" and h.manifest()["incumbent"] == h.initial
    assert len(h.manifest()["reports"]) == 1
    assert lab.recovery_status(h.campaign)["recovery_report"]["state"] == "complete"
    assert len(list(attempt.glob("report.pre-recovery-*.json"))) == 1


def test_multiple_recovery_generations_preserve_paid_review_usage(h, monkeypatch):
    publish = lab.publish
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(lab, "publish", interrupt)
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            h.run()
    monkeypatch.setattr(lab, "publish", publish)
    report = h.run()
    assert report["outcome"] == "accepted" and report["verification_generation"] == 2
    assert len(report["usage"]) == 4  # One implementation and three independent review sessions.
    assert len({s["session_id"] for s in report["usage"]}) == 4
    assert all(s["usage_status"] == "provider_reported" and s["usage"] == [{"input_tokens": 10}]
               for s in report["usage"])
    assert len(h.manifest()["reports"]) == 1
