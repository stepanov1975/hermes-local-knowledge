"""Synthetic admission/concurrency tests, never model or messaging calls."""
import os
import subprocess
import sys
import threading

import pytest

from scripts import lab_automation as auto
from scripts import lab_experiment as lab
from scripts import lab_recovery as recovery
from tests.test_lab_experiment import Harness


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    auto.initialize(harness.campaign)
    # Harness sessions omit Docker resource receipts; retention has dedicated real
    # filesystem/Git and integrated lifecycle tests, not fabricated proof here.
    monkeypatch.setattr(auto, "maintain", lambda *a: {})
    return harness


def test_hour_reservation_backward_skip_and_crash(h):
    calls = []
    def action(campaign, **kwargs):
        calls.append(kwargs)
        assert auto.status(campaign)["admission"]["hour"] == 20
        assert recovery.lock_fds()
        with pytest.raises(BlockingIOError), lab.campaign_lock(campaign):
            pass
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        auto.tick(h.campaign, now=20 * 3600, action=action)
    for hour in (20, 19, 0):
        assert auto.tick(h.campaign, now=hour * 3600, action=action)["state"] == "hour_skipped"
    assert len(calls) == 1


def test_busy_immediate_and_not_body_blocking_error(h):
    with lab.campaign_lock(h.campaign):
        assert auto.tick(h.campaign, now=3600)["state"] == "busy"
    assert not auto.status(h.campaign)["admission"]
    def action(*args, **kwargs):
        raise BlockingIOError("private provider detail")
    with pytest.raises(BlockingIOError):
        auto.tick(h.campaign, now=3600, action=action)
    assert auto.status(h.campaign)["failure"]["kind"] == "operational_failure"
    assert "private provider" not in (auto.root(h.campaign) / "failure.json").read_text()
    assert auto.tick(h.campaign, now=7200)["state"] == "paused"


def test_pending_recovery_once_and_safety_pause(h):
    manifest = h.manifest()
    manifest["pending"] = "synthetic-pending"
    lab.durable_json(h.campaign / "manifest.json", manifest, replace=True)
    calls = []
    def action(campaign, **kwargs):
        calls.append(kwargs)
        lab.durable_json(campaign / "manifest.json", {**manifest, "pending": None}, replace=True)
        return {"outcome": "interrupted"}
    assert auto.tick(h.campaign, now=3600, action=action)["action"] == "recover"
    assert calls == [{"recover_only": True}]
    lab.durable_json(h.campaign / "manifest.json", {**manifest, "paused": True,
                     "pause_source": "recovery"}, replace=True)
    auto.manual_pause(h.campaign, True)
    auto.manual_pause(h.campaign, False)
    assert auto.tick(h.campaign, now=7200, action=action)["state"] == "paused"
    assert len(calls) == 1


@pytest.mark.parametrize("change,approved,outcome", [(False, True, "no_change"), (True, False, "rejected")])
def test_real_controller_same_lock_ordinary_outcomes_continue(h, change, approved, outcome):
    h.change, h.review_approved = change, approved
    def action(campaign, **kwargs):
        return lab.run_under_existing_lock(campaign, session=h.session, evaluator=h.evaluator,
                    checks=h.checks, clearance=lambda *_: True, loader=h.loader, **kwargs)
    for hour in (1, 2):
        h.strategy = f"synthetic strategy with distinct premise {hour}"
        assert auto.tick(h.campaign, now=hour * 3600, action=action)["result"]["outcome"] == outcome
    assert len(h.manifest()["reports"]) == 2


def test_pause_and_resume_while_provider_blocked_no_launch(h):
    entered, release = threading.Event(), threading.Event()
    errors = []
    def action(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {}
    def worker():
        try:
            auto.tick(h.campaign, now=3600, action=action)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(5)
    try:
        assert auto.manual_pause(h.campaign, True)["manual_paused"] is True
        assert auto.tick(h.campaign, now=7200)["state"] == "busy"
        assert auto.manual_pause(h.campaign, False)["manual_paused"] is False
        assert h.calls == []
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors == []


def test_existing_lock_rejects_other_campaign(h):
    with lab.campaign_lock(h.campaign):
        pass
    other = h.root / "other-campaign"
    other.mkdir()
    (other / "lock").touch()
    with lab.campaign_lock(other), pytest.raises(ValueError, match="another campaign"):
        lab.run_under_existing_lock(h.campaign)


def test_inherited_fd_retains_campaign_flock(h):
    child = None
    with lab.campaign_lock(h.campaign):
        fd, = recovery.lock_fds()
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE, pass_fds=(fd,))
    try:
        assert auto.tick(h.campaign, now=3600)["state"] == "busy"
    finally:
        child.communicate(timeout=5)
    with lab.campaign_lock(h.campaign):
        assert recovery.lock_fds()


def test_status_read_only_and_private_modes(h):
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in h.campaign.rglob("*") if p.is_file()}
    auto.status(h.campaign)
    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in h.campaign.rglob("*") if p.is_file()}
    assert before == after
    assert os.stat(auto.root(h.campaign)).st_mode & 0o777 == 0o700
    for path in auto.root(h.campaign).iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_existing_lock_helper_requires_lock(h):
    with pytest.raises(ValueError, match="Existing campaign lock"):
        lab.run_under_existing_lock(h.campaign)
