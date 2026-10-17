"""Review regressions: authoritative usage paths and independent pause ordering."""
import json
import threading

import pytest

from scripts import lab_disk_guard as disk
from scripts import lab_experiment as lab
from scripts import lab_recovery as recovery
from tests.test_lab_experiment import Harness
from tests.test_lab_recovery import pending_candidate


@pytest.mark.parametrize("image", ["sha256:" + "a" * 64, "sha256:" + "f" * 64])
def test_init_rejects_noncanonical_image_before_creating_campaign(h, image):
    target = h.root / "invalid-image"
    config = {**h.config, "image": image}
    with pytest.raises(ValueError, match="fixed agent image"):
        lab.init_campaign(target, config, loader=h.loader)
    assert not target.exists()
    assert config["image"] == image


@pytest.mark.parametrize("owner,alias", [("configured", False), ("configured", True),
                                        ("controller", False), ("controller", True)])
def test_init_rejects_repository_campaign_before_any_write(h, monkeypatch, owner, alias):
    from pathlib import Path
    controller = h.root / "controller"
    (controller / "scripts").mkdir(parents=True)
    monkeypatch.setattr(lab, "__file__", str(controller / "scripts/lab_experiment.py"))
    root = h.repo if owner == "configured" else controller
    if alias:
        link = h.root / "checkout-alias"
        link.symlink_to(root, target_is_directory=True)
        target = link / "private-campaign"
    else:
        target = root / "private-campaign"
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="outside.*repository"):
        lab.init_campaign(target, h.config, loader=h.loader)
    assert not target.exists()
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert Path(h.config["repo"]) == h.repo


@pytest.fixture
def absent_session_helper(monkeypatch):
    """Model a controller-planned helper and successful exact-name absence lookup."""
    names = set()

    def inspect(name):
        assert name in names, "Only this fixture's planned helpers may be inspected"
        return None  # Successful exact-name lookup: helper has already exited/been removed.

    monkeypatch.setattr(recovery, "docker_inspect", inspect)

    def plan(session, image):
        name = f"lk-lab-{session.parent.name}-{session.name}-setup"
        args = ["docker", "run", "--rm", "--name", name, "--label", "lk.lab=ticket-69",
                "--network", "none", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                "--security-opt", "no-new-privileges:true", "--read-only",
                "--mount", f"type=bind,src={session / 'work'},dst=/work",
                "--entrypoint", "/bin/chown", image, "-R", "--no-dereference", "10000:10000", "/work"]
        # The controller resource context is active around the injected session.
        assert recovery.plan_resource(args, image) is not None
        names.add(name)

    return plan


@pytest.mark.parametrize("unanswered", [False, True])
def test_disk_paused_pending_reconciles_only_after_fresh_space_check(h, monkeypatch, unanswered,
                                                                  absent_session_helper):
    from scripts import lab_automation as automation
    low = [False]
    monkeypatch.setattr(disk, "available_space", lambda path: {
        "available_blocks": 9 if low[0] else 20, "usable_blocks": 100})
    def interrupted(**kwargs) -> dict:
        private = kwargs["attempt"] / "private"
        private.mkdir(parents=True)
        absent_session_helper(kwargs["attempt"], kwargs["image"])
        lab.durable_json(private / "session.json", {"session_id": "synthetic-disk-session", **lab.MODEL})
        if unanswered:
            recovery.atomic_json(private / "wire-1.json", [{"role": "user", "content": "synthetic"}])
        low[0] = True
        guard = disk._CURRENT.get()
        assert guard is not None
        guard.sample()
        disk.checkpoint()
        raise AssertionError("Unsafe disk checkpoint returned")
    with pytest.raises(disk.LabDiskPaused):
        lab.run_experiment(h.campaign, session=interrupted, loader=h.loader)
    pending = h.manifest()["pending"]
    assert pending and h.manifest()["paused"]
    with pytest.raises(disk.LabDiskPaused):
        h.run(recover_only=True)
    with pytest.raises(ValueError, match="pending"):
        automation.acknowledge_blockers(h.campaign)
    assert h.manifest()["pending"] == pending
    low[0] = False
    if unanswered:
        with pytest.raises(ValueError, match="model_completion_unknown"):
            h.run(recover_only=True)
        assert h.manifest()["pending"] == pending and h.manifest()["pause_source"] == "recovery"
        assert not h.calls and not h.evaluations
        with pytest.raises(ValueError, match="pending"):
            automation.acknowledge_blockers(h.campaign)
        return  # Never synthesize an answer to unblock a genuinely unanswered call.
    report = h.run(recover_only=True)
    assert report["attempt_id"] == pending and report["outcome"] == "error"
    assert h.manifest()["pending"] is None
    assert not h.calls and not h.evaluations
    monkeypatch.setattr(lab, "validate_config", lambda config: h.loader())
    automation.initialize(h.campaign)
    automation.acknowledge_blockers(h.campaign)
    assert not h.manifest()["paused"] and not h.calls


@pytest.mark.parametrize("receipt", ["wire"])
@pytest.mark.parametrize("role", ["implementation", "review"])
def test_response_loss_after_provider_acceptance_stays_pending_despite_local_cleanup(
        h, receipt, role, absent_session_helper):
    from scripts import lab_automation as automation
    accepted = []
    original_response = {"message": {"role": "assistant", "content": "Synthetic accepted response"},
                         "usage": {"input_tokens": 3, "output_tokens": 2}}
    def lost(**kwargs):
        if kwargs["role"] != role:
            return h.session(**kwargs)
        private = kwargs["attempt"] / "private"
        private.mkdir(parents=True)
        absent_session_helper(kwargs["attempt"], kwargs["image"])
        metadata = {"session_id": "accepted-response-lost", "role": role, **lab.MODEL}
        lab.durable_json(private / "session.json", metadata)
        recovery.atomic_json(private / (receipt + "-1.json"),
                             [{"role": "user", "content": "Synthetic dispatch intent"}])
        accepted.append(role)  # Synthetic provider accepted; response never reached controller.
        lab.durable_json(private / "status.json", {**metadata, "cleanup_complete": True, "usage": []})
        raise ConnectionError("synthetic response loss after acceptance")
    with pytest.raises(ConnectionError):
        lab.run_experiment(h.campaign, session=lost, evaluator=h.evaluator, checks=h.checks, loader=h.loader)
    manifest = h.manifest()
    assert manifest["pending"] and manifest["paused"] and manifest["pause_source"] == "recovery"
    attempt = h.campaign / "attempts" / manifest["pending"]
    assert lab.read_json(attempt / "report.json")["reconciliation_required"] is True
    with pytest.raises(ValueError, match="pending"):
        automation.acknowledge_blockers(h.campaign)
    calls = len(h.calls)
    with pytest.raises(ValueError, match="model_completion_unknown"):
        h.run(recover_only=True)
    assert h.manifest()["pending"] == attempt.name and len(h.calls) == calls and accepted == [role]
    assert not (attempt / role / "private/response-1.json").exists()
    # Only a genuinely recovered original response can settle this receipt pair;
    # no API fabricates one and no automatic retry is performed.
    lab.durable_json(attempt / role / "private/response-1.json", original_response)
    settled = h.run(recover_only=True)
    assert settled["outcome"] == "error" and h.manifest()["pending"] is None
    assert len(h.calls) == calls and accepted == [role]


@pytest.mark.parametrize("pause_source", [None, "manual", "auth"])
def test_explicit_recovery_cannot_bypass_independent_pause_with_stale_disk_alert(h, pause_source):
    pending_candidate(h)
    manifest = {**h.manifest(), "paused": True}
    if pause_source:
        manifest["pause_source"] = pause_source
    lab.durable_json(h.campaign / "manifest.json", manifest, replace=True)
    lab.durable_json(h.campaign / "disk-alert.json", {"kind": "lab_disk_paused"})
    calls = len(h.calls)
    with pytest.raises(ValueError, match="paused or pending"):
        h.run(recover_only=True)
    assert h.manifest() == manifest and len(h.calls) == calls


def test_auth_failure_remains_independently_paused_during_recovery(h):
    h.failure = type("AuthenticationError", (RuntimeError,), {})("synthetic auth failure")
    with pytest.raises(RuntimeError):
        h.run()
    manifest = h.manifest()
    assert manifest["paused"] and manifest["pending"] and "pause_source" not in manifest
    with pytest.raises(ValueError, match="paused or pending"):
        h.run(recover_only=True)
    assert h.manifest() == manifest and len(h.calls) == 1


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


@pytest.mark.parametrize("directory", ["scratch", "output"])
def test_recovery_usage_ignores_worker_owned_result_files(h, directory):
    attempt = pending_candidate(h)
    root = attempt / "implementation" / directory
    root.mkdir(parents=True, exist_ok=True)
    (root / "benchmark-result.json").write_text('[{"passed": true}]')
    (root / "forged-result.json").write_text(json.dumps({
        "role": "review", "session_id": "worker-output-not-authority",
        "usage": [{"input_tokens": 999999}]}))
    report = h.run()
    assert report["outcome"] == "accepted" and report["attempt_id"] == attempt.name
    assert len(report["usage"]) == 2
    assert all(s["session_id"] != "worker-output-not-authority" for s in report["usage"])
    assert all(s["usage"] == [{"input_tokens": 10}] for s in report["usage"])


@pytest.mark.parametrize("directory", ["scratch", "output"])
def test_completion_scan_ignores_worker_private_directories(tmp_path, directory):
    private = tmp_path / "implementation" / directory / "private"
    private.mkdir(parents=True)
    for name in ("request-1.json", "wire-1.json", "tool-intent-1-0.json"):
        (private / name).write_text("{}")
    assert recovery.session_completion(tmp_path) == "settled"


def test_recovery_pause_publication_serializes_concurrent_disk_pause(h, monkeypatch):
    attempt = pending_candidate(h)
    calls = len(h.calls)
    low = [False]
    monkeypatch.setattr(disk, "available_space", lambda path: {
        "available_blocks": 9 if low[0] else 20, "usable_blocks": 100})
    def uncertain(path):
        assert path == attempt
        raise recovery.RecoveryPaused("resource_inspection_unavailable")
    monkeypatch.setattr(recovery, "settle_resources", uncertain)
    durable = lab.durable_json
    samplers = []
    entered, completed = threading.Event(), threading.Event()
    sampled_during_write = []
    def write(path, value, *, replace=False):
        if path.name == "recovery-alert.json":
            guard = disk._CURRENT.get()
            assert guard is not None
            low[0] = True
            def sample():
                entered.set()
                guard.sample()
                completed.set()
            sampler = threading.Thread(target=sample)
            samplers.append(sampler)
            sampler.start()
            assert entered.wait(5)
            sampled_during_write.append(completed.wait(0.1))
        durable(path, value, replace=replace)
    monkeypatch.setattr(lab, "durable_json", write)
    with pytest.raises(ValueError, match="resource_inspection_unavailable"):
        h.run()
    for sampler in samplers:
        sampler.join(timeout=5)
        assert not sampler.is_alive()
    assert sampled_during_write == [False] and completed.is_set()
    assert h.manifest()["paused"] and h.manifest().get("pause_source") != "recovery"
    low[0] = False
    monkeypatch.setattr(lab, "durable_json", durable)
    with pytest.raises(ValueError, match="resource_inspection_unavailable"):
        h.run(recover_only=True)
    assert h.manifest()["pause_source"] == "recovery"
    assert h.manifest()["pending"] == attempt.name and len(h.calls) == calls
    assert h.manifest()["reports"] == []
