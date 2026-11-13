"""Integrated hourly/retention controls with real filesystem/Git, synthetic work."""
from __future__ import annotations

import pytest

from scripts import lab_automation as auto
from scripts import lab_disk_guard as disk
from scripts import lab_experiment as lab
from scripts import lab_notifications as notes
from scripts import lab_retention as retention
from tests.test_lab_experiment import Harness
from tests.test_lab_retention import finished as finished


def test_paused_tick_runs_retention_and_expiry_without_admission(finished):
    h, attempt = finished
    auto.initialize(h.campaign)
    auto.manual_pause(h.campaign, True)
    def forbidden(*args, **kwargs) -> dict:
        pytest.fail("Manual pause must prevent new work")
    assert auto.tick(h.campaign, now=100, action=forbidden)["state"] == "paused"
    assert not (attempt / "implementation/work").exists()
    assert (attempt / "implementation/private/request-1.json").exists()
    assert auto.status(h.campaign)["admission"] == {}
    assert auto.tick(h.campaign, now=100 + retention.RAW_RETENTION_SECONDS,
                     action=forbidden)["state"] == "paused"
    assert not (attempt / "implementation/private/request-1.json").exists()
    assert (attempt / "report.json").is_file()
    assert auto.status(h.campaign)["retention"]["blocked"] == []


def test_retention_blocker_prevents_launch_and_notifies(finished):
    h, attempt = finished
    auto.initialize(h.campaign)
    (attempt / "resources/owned.json").unlink()
    calls = []
    def unexpected(*args, **kwargs) -> dict:
        calls.append(True)
        return {}
    assert auto.tick(h.campaign, now=100, action=unexpected)["state"] == "paused"
    assert calls == [] and (attempt / "implementation/work").exists()
    assert auto.status(h.campaign)["failure"]["kind"] == "retention_blocked"
    box = notes.scan(h.campaign)
    assert [e["kind"] for e in box["events"].values()].count("controller_failure") == 1
    assert notes.scan(h.campaign) == box


def test_low_disk_pauses_before_disposal_and_busy_scan_observes_it(finished, monkeypatch):
    h, attempt = finished
    auto.initialize(h.campaign)
    monkeypatch.setattr(disk, "available_space", lambda _: {"available_blocks": 9, "usable_blocks": 100})
    with pytest.raises(disk.LabDiskPaused):
        auto.tick(h.campaign, now=100)
    assert h.manifest()["paused"] and (attempt / "implementation/work").exists()
    assert auto.status(h.campaign)["admission"] == {}
    kinds = [e["kind"] for e in notes.scan(h.campaign)["events"].values()]
    assert kinds.count("blocker") == 1 and "controller_failure" not in kinds
    with lab.campaign_lock(h.campaign):
        assert auto.tick(h.campaign, now=200)["state"] == "busy"
    assert notes.scan(h.campaign)["events"]


def test_explicit_ack_preserves_failure_and_does_not_launch(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    auto.initialize(h.campaign)
    auto.manual_pause(h.campaign, True)
    failure = {"kind": "maintenance_failure", "observed_at": 100}
    lab.durable_json(auto.root(h.campaign) / "failure.json", failure)
    lab.durable_json(h.campaign / "manifest.json", {**h.manifest(), "paused": True}, replace=True)
    monkeypatch.setattr(lab, "validate_config", lambda _: {"synthetic": True})
    result = auto.acknowledge_blockers(h.campaign)
    assert not result["manual_paused"] and not result["safety_paused"] and not result["failure"]
    archives = list(auto.root(h.campaign).glob("failure-acknowledged-*.json"))
    assert len(archives) == 1 and lab.read_json(archives[0]) == failure
    assert h.calls == [] and auto.status(h.campaign)["admission"] == {}


def test_ack_cannot_bypass_pending_or_low_disk(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    auto.initialize(h.campaign)
    current = h.manifest()
    lab.durable_json(h.campaign / "manifest.json", {**current, "pending": "uncertain", "paused": True}, replace=True)
    with pytest.raises(ValueError, match="Reconcile pending"):
        auto.acknowledge_blockers(h.campaign)
    assert h.manifest()["pending"] == "uncertain"
    lab.durable_json(h.campaign / "manifest.json", {**current, "paused": True}, replace=True)
    monkeypatch.setattr(disk, "available_space", lambda _: {"available_blocks": 9, "usable_blocks": 100})
    with pytest.raises(disk.LabDiskPaused):
        auto.acknowledge_blockers(h.campaign)
    assert h.manifest()["paused"]


def test_post_action_cleanup_shares_existing_flock(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    auto.initialize(h.campaign)
    observations = []
    def cleanup(campaign, *, now):
        with pytest.raises(BlockingIOError), lab.campaign_lock(campaign):
            pass
        observations.append(now)
        return {"cleaned": [], "blocked": []}
    monkeypatch.setattr(retention, "cleanup_finished", cleanup)
    def action(campaign, **kwargs):
        with pytest.raises(BlockingIOError), lab.campaign_lock(campaign):
            pass
        assert kwargs == {"recover_only": False}
        return {"outcome": "no_change"}
    assert auto.tick(h.campaign, now=3600, action=action)["state"] == "completed"
    assert observations == [3600, 3600]
