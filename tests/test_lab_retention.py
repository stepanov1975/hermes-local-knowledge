"""Real temporary filesystem/Git retention proofs; no Docker/provider execution."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts import lab_experiment as lab
from scripts import lab_retention as retention
from tests.test_lab_experiment import Harness


@pytest.fixture
def finished(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    report = h.run()
    attempt = h.campaign / "attempts" / report["attempt_id"]
    (attempt / "resources").mkdir()
    lab.durable_json(attempt / "resources/owned.json", {
        "image": h.config["image"],
        "args": ["docker", "create", "--name", "lk-lab-" + attempt.name,
                 "--label", "lk.lab=ticket-69", "--entrypoint", "/bin/sleep", h.config["image"], "infinity"]})
    monkeypatch.setattr(retention.recovery, "docker_inspect", lambda _: None)
    monkeypatch.setattr(retention.disk, "available_space", lambda _: {
        "available_blocks": 50, "usable_blocks": 100})
    private = attempt / "implementation/private"
    private.mkdir()
    for name, value in {
        "request-1.json": {"messages": []}, "wire-1.json": {"body": {}},
        "response-1.json": {"usage": {"input_tokens": 12}, "message": {}},
        "status.json": {"complete": True, "cleanup_complete": True, "usage": [{"input_tokens": 12}]},
        "tool-intent-1-0.json": {"command": "private"},
        "tool-1-0.json": {"exit_code": 0, "stored_bytes": {"stdout": 3}},
        "tool-1-0.output.json": {"exit_code": 0},
    }.items():
        lab.durable_json(private / name, value)
    for name in ("tool-1-0.stdout", "tool-1-0.stderr"):
        (private / name).write_text("raw")
    (private / "unknown-transcript.json").write_text("preserve unknown")
    for name in ("home", "scratch", "output", "inputs"):
        root = attempt / "implementation" / name
        root.mkdir()
        (root / "data").write_text("disposable")
    (attempt / "candidate.index").write_text("index")
    return h, attempt


def clean(h, now: float = 100):
    return retention.cleanup_campaign(h.campaign, now=now)


def test_disposal_preserves_report_git_inputs_and_structural_records(finished):
    h, attempt = finished
    report = (attempt / "report.json").read_bytes()
    manifest = (h.campaign / "manifest.json").read_bytes()
    outcome = clean(h)
    assert outcome["blocked"] == []
    assert not (attempt / "implementation/work").exists()
    assert not (attempt / "candidate.index").exists()
    assert (attempt / "report.json").read_bytes() == report
    assert (h.campaign / "manifest.json").read_bytes() == manifest
    assert (h.campaign / "check-tools/provenance.json").is_file()
    plan = lab.read_json(attempt / "retention-plan.json")
    revision = lab.read_json(attempt / "report.json")["revision"]
    assert lab.git(h.campaign / "repo.git", "rev-parse", plan["refs"][revision]).decode().strip() == revision
    assert (attempt / "implementation/private/request-1.json").is_file()
    assert plan["usage"]["implementation/private/response-1.json"] == {"input_tokens": 12}


def test_raw_exact_seven_days_from_durable_receipt_not_mtime(finished):
    h, attempt = finished
    raw = attempt / "implementation/private/request-1.json"
    os.utime(raw, (0, 0))
    assert clean(h)["blocked"] == []
    cutoff = 100 + retention.RAW_RETENTION_SECONDS
    assert clean(h, cutoff - 0.01)["blocked"] == []
    assert raw.exists()
    os.utime(raw, (cutoff + 1000, cutoff + 1000))
    assert clean(h, cutoff)["blocked"] == []
    assert not raw.exists()
    private = raw.parent
    for name in ("status.json", "tool-1-0.json", "tool-intent-1-0.json",
                 "tool-1-0.output.json", "unknown-transcript.json"):
        assert (private / name).is_file()
    before = (attempt / "retention-plan.json").read_bytes()
    assert clean(h, cutoff + 10)["blocked"] == []
    assert (attempt / "retention-plan.json").read_bytes() == before
    assert lab.read_json(attempt / "retention-result.json")["raw_complete"] is True


@pytest.mark.parametrize("fault", ["missing", "drift", "revision", "pending", "reconcile"])
def test_ineligible_finalized_attempt_retains_everything(finished, fault):
    h, attempt = finished
    manifest = h.manifest()
    report = lab.read_json(attempt / "report.json")
    if fault == "missing":
        (attempt / "report.json").unlink()
    elif fault == "drift":
        (attempt / "report.json").write_text("{}")
    else:
        if fault == "revision":
            report["revision"] = "f" * 40
        elif fault == "reconcile":
            report["reconciliation_required"] = True
        else:
            manifest["pending"] = attempt.name
            report["outcome"] = "error"
        lab.durable_json(attempt / "report.json", report, replace=True)
        manifest["reports"][-1]["sha256"] = lab.file_sha(attempt / "report.json")
        lab.durable_json(h.campaign / "manifest.json", manifest, replace=True)
    assert clean(h)["blocked"]
    assert (attempt / "implementation/work/plugin").is_dir()
    assert not (attempt / "retention-plan.json").exists()


@pytest.mark.parametrize("state", ["running", "unknown", "foreign", "unavailable"])
def test_unknown_live_foreign_resources_retained(finished, monkeypatch, state):
    h, attempt = finished
    def inspect(_):
        if state in {"foreign", "unavailable"}:
            raise retention.recovery.RecoveryPaused("resource_identity_mismatch")
        return [{"name": "exact", "state": state}]
    monkeypatch.setattr(retention.recovery, "inspect_resources", inspect)
    assert clean(h)["blocked"]
    assert (attempt / "implementation/work").exists()


def test_stopped_exact_resource_settled_and_read_back(finished, monkeypatch):
    h, _ = finished
    state = ["stopped"]
    def settle(_):
        state[0] = "absent"
        return []
    monkeypatch.setattr(retention.recovery, "inspect_resources", lambda _: [{"state": state[0]}])
    monkeypatch.setattr(retention.recovery, "settle_resources", settle)
    assert clean(h)["blocked"] == []
    assert state == ["absent"]


@pytest.mark.parametrize("parent", ["work", "private"])
def test_top_level_symlink_foreign_target_retained(finished, tmp_path, parent):
    h, attempt = finished
    root = attempt / "implementation" / parent
    root.rename(root.with_name(parent + "-original"))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("foreign")
    root.symlink_to(foreign, target_is_directory=True)
    assert clean(h)["blocked"]
    assert (foreign / "keep").read_text() == "foreign"
    assert (attempt / "implementation/work-original" if parent == "work" else attempt / "implementation/work").is_dir()


def test_sandbox_internal_symlink_removed_without_following(finished, tmp_path):
    h, attempt = finished
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("keep")
    (attempt / "implementation/scratch/link").symlink_to(foreign, target_is_directory=True)
    assert clean(h)["blocked"] == []
    assert (foreign / "keep").read_text() == "keep"


def test_missing_response_retains_pairs_and_workspace(finished):
    h, attempt = finished
    (attempt / "implementation/private/response-1.json").unlink()
    assert clean(h)["blocked"]
    assert (attempt / "implementation/work").exists()
    assert (attempt / "implementation/private/request-1.json").exists()


def test_interrupted_disposal_retries_original_plan(finished, monkeypatch):
    h, attempt = finished
    original = Path.unlink
    deletions = [0]
    def interrupt(path, *args, **kwargs):
        if "/implementation/work/" in str(path):
            deletions[0] += 1
            if deletions[0] == 2:
                raise KeyboardInterrupt
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupt)
        with pytest.raises(KeyboardInterrupt):
            clean(h)
    plan = (attempt / "retention-plan.json").read_bytes()
    assert clean(h, 200)["blocked"] == []
    assert (attempt / "retention-plan.json").read_bytes() == plan
    assert not (attempt / "implementation/work").exists()


def test_unplanned_data_after_interruption_is_not_reclassified(finished, monkeypatch):
    h, attempt = finished
    with monkeypatch.context() as patch:
        patch.setattr(retention, "_dispose", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            clean(h)
    (attempt / "implementation/work/new").write_text("unknown")
    assert clean(h)["blocked"]
    assert (attempt / "implementation/work/new").exists()


def test_structural_drift_blocks_expired_raw_disposal(finished):
    h, attempt = finished
    assert clean(h)["blocked"] == []
    (attempt / "implementation/private/status.json").write_text("{}")
    assert clean(h, 100 + retention.RAW_RETENTION_SECONDS)["blocked"]
    assert (attempt / "implementation/private/response-1.json").exists()


def test_interrupted_expired_raw_pair_disposal_reuses_completion(finished, monkeypatch):
    h, attempt = finished
    assert clean(h)["blocked"] == []
    cutoff = 100 + retention.RAW_RETENTION_SECONDS
    original = Path.unlink
    calls = [0]
    def interrupt(path, *args, **kwargs):
        if path.parent.name == "private" and path.suffix == ".json":
            calls[0] += 1
            if calls[0] == 2:
                raise KeyboardInterrupt
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupt)
        with pytest.raises(KeyboardInterrupt):
            clean(h, cutoff)
    assert clean(h, cutoff + 1)["blocked"] == []
    assert not (attempt / "implementation/private/request-1.json").exists()
    assert (attempt / "implementation/private/status.json").is_file()


def test_missing_resource_receipt_blocks_disposal(finished):
    h, attempt = finished
    (attempt / "resources/owned.json").unlink()
    assert clean(h)["blocked"]
    assert (attempt / "implementation/work").exists()


def test_native_result_and_orphan_archives_survive(finished):
    h, attempt = finished
    output = attempt / "evaluation-candidate/output"
    output.mkdir(parents=True)
    lab.durable_json(output / "native-baseline.json", {"complete": True})
    archived = attempt / ("report.pre-recovery-" + "a" * 64 + ".json")
    lab.durable_json(archived, {"revision": h.initial, "outcome": "accepted"})
    before = archived.read_bytes()
    assert clean(h)["blocked"] == []
    assert clean(h, 100 + retention.RAW_RETENTION_SECONDS)["blocked"] == []
    assert archived.read_bytes() == before
    assert (output / "native-baseline.json").is_file()


def test_unknown_disk_retains_all_data(finished, monkeypatch):
    h, attempt = finished
    def unavailable(_):
        raise OSError("unknown sample")
    monkeypatch.setattr(retention.disk, "available_space", unavailable)
    with pytest.raises(OSError):
        clean(h)
    assert (attempt / "implementation/work").exists()


def test_actual_foreign_resource_spec_blocks_disposal(finished, monkeypatch):
    h, attempt = finished
    monkeypatch.setattr(retention.recovery, "docker_inspect", lambda _: {
        "Name": "/foreign", "Image": "foreign", "Mounts": [],
        "State": {"Running": False}, "Id": "foreign",
        "Config": {"Labels": {}, "Cmd": [], "Entrypoint": []}})
    result = clean(h)
    assert result["blocked"][0]["reason"] == "resource_identity_mismatch"
    assert (attempt / "implementation/work").exists()


def test_low_space_never_triggers_cleanup(finished, monkeypatch):
    h, attempt = finished
    monkeypatch.setattr(retention.disk, "available_space", lambda _: {
        "available_blocks": 9, "usable_blocks": 100})
    with pytest.raises(retention.RetentionBlocked, match="disk_unsafe"):
        clean(h)
    assert (attempt / "implementation/work").exists()


@pytest.mark.parametrize("generation", ["", "generation-1/"])
@pytest.mark.parametrize("role", ["evaluation-incumbent", "evaluation-candidate"])
def test_actual_evaluator_raw_expires_but_reproducibility_evidence_survives(finished, generation, role):
    h, attempt = finished
    root = attempt / generation / role
    root.mkdir(parents=True)
    raw = {name: b"private execution bytes" for name in (
        "trusted-tests.stdout", "trusted-tests.stderr", "transport.stdout", "transport.stderr",
        "private-cases.jsonl", "warmups.json")}
    for name, value in raw.items():
        (root / name).write_bytes(value)
    structural = {
        "aggregate.json": {"complete": True, "development": {"hit_at_5": 1}},
        "runner.json": {"trusted_tests": {"test_plugin.py": "source hash"}},
        "trusted-tests.output.json": {"exit_code": 0, "stored_bytes": {"stdout": 23}},
        "transport.stderr.json": {"stored_bytes": 23},
        "cleanup.json": {"worker_removed": True},
        "output/native-baseline.json": {"integrity": "ok"},
        "output/coverage.json": {"missing": []},
        "output/index-identity.json": {"identity": "pinned"},
    }
    for name, value in structural.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        lab.durable_json(root / name, value)
    tests = root / "trusted-tests/test_plugin.py"
    tests.parent.mkdir()
    tests.write_text("# pinned controller test source\n")
    preserved = {name: (root / name).read_bytes() for name in [*structural, "trusted-tests/test_plugin.py"]}
    assert clean(h)["blocked"] == []
    plan = lab.read_json(attempt / "retention-plan.json")
    for name, value in raw.items():
        key = (root / name).relative_to(attempt).as_posix()
        assert plan["raw"][key]["sha256"] == lab.file_sha(root / name)
        assert (root / name).read_bytes() == value
        assert key not in plan["protected"]
    for name in preserved:
        key = (root / name).relative_to(attempt).as_posix()
        assert plan["protected"][key] == lab.file_sha(root / name)
    assert plan["completion"] == "settled"
    assert plan["usage"]["implementation/private/response-1.json"] == {"input_tokens": 12}
    assert clean(h, 100 + retention.RAW_RETENTION_SECONDS - 0.01)["blocked"] == []
    assert all((root / name).is_file() for name in raw)
    assert clean(h, 100 + retention.RAW_RETENTION_SECONDS)["blocked"] == []
    assert all(not (root / name).exists() for name in raw)
    assert all((root / name).read_bytes() == value for name, value in preserved.items())
    assert (attempt / "retention-plan.json").stat().st_mode & 0o777 == 0o600


def test_evaluator_aggregate_drift_blocks_expired_raw_disposal(finished):
    h, attempt = finished
    root = attempt / "evaluation-candidate"
    root.mkdir()
    lab.durable_json(root / "aggregate.json", {"complete": True})
    (root / "private-cases.jsonl").write_text("raw")
    assert clean(h)["blocked"] == []
    (root / "aggregate.json").write_text("{}")
    result = clean(h, 100 + retention.RAW_RETENTION_SECONDS)
    assert result["blocked"][0]["reason"] == "structural_evidence_drift"
    assert (root / "private-cases.jsonl").read_text() == "raw"


def test_cleanup_finished_requires_existing_campaign_lock(finished):
    h, attempt = finished
    with pytest.raises(retention.RetentionBlocked, match="campaign_lock_required"):
        retention.cleanup_finished(h.campaign, now=100)
    assert (attempt / "implementation/work").exists()
    assert not (attempt / "retention-plan.json").exists()
    with lab.campaign_lock(h.campaign):
        assert retention.cleanup_finished(h.campaign, now=100)["blocked"] == []
    assert not (attempt / "implementation/work").exists()


@pytest.mark.parametrize("substitution", ["other_campaign", "replaced_inode"])
def test_cleanup_finished_rejects_wrong_lock_before_mutation(finished, tmp_path, substitution):
    h, attempt = finished
    other = tmp_path / "other-campaign"
    other.mkdir()
    with lab.campaign_lock(other if substitution == "other_campaign" else h.campaign):
        if substitution == "replaced_inode":
            (h.campaign / "lock").rename(h.campaign / "lock-original")
            (h.campaign / "lock").touch()
        with pytest.raises(retention.RetentionBlocked, match="campaign_lock_mismatch"):
            retention.cleanup_finished(h.campaign, now=100)
    assert (attempt / "implementation/work").exists()
    assert not (attempt / "retention-plan.json").exists()
