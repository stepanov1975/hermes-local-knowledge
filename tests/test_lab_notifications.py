"""Real durable outbox with fake public CLI subprocess boundary; no real sends."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import lab_automation as auto
from scripts import lab_experiment as lab
from scripts import lab_notifications as notes
from tests.test_lab_experiment import Harness


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    auto.initialize(harness.campaign)
    return harness


def event(h, *, outcome="accepted"):
    attempt_id = "synthetic-attempt"
    attempt = h.campaign / "attempts" / attempt_id
    attempt.mkdir(exist_ok=True)
    path = attempt / "report.json"
    lab.durable_json(path, {"attempt_id": attempt_id, "outcome": outcome}, replace=True)
    manifest = h.manifest()
    lab.durable_json(h.campaign / "manifest.json", {**manifest,
        "reports": [{"attempt_id": attempt_id, "sha256": lab.file_sha(path)}]}, replace=True)


def delivery(h, runner):
    # Validate a system executable, not a temporary file on pinned noexec /tmp.
    executable = Path("/usr/bin/true")
    home = h.root / "messaging"
    home.mkdir(exist_ok=True)
    return notes.deliver(h.campaign, executable=executable, target="telegram:-123:456",
                         home=home, hermes_home=home, runner=runner)


def test_authoritative_crash_scan_and_quiet_outcomes(h):
    for outcome in ("no_change", "rejected"):
        event(h, outcome=outcome)
        assert notes.scan(h.campaign)["events"] == {}
    event(h)
    first = notes.scan(h.campaign)
    assert len(first["events"]) == 1
    assert notes.scan(h.campaign) == first
    for row in first["events"].values():
        assert "synthetic-attempt" not in row["text"]


@pytest.mark.parametrize("payload,code,state", [
    ({"success": True, "message_id": 789}, 0, "sent"),
    ({"success": True}, 0, "uncertain"),
    ({"success": True, "skipped": True, "message_id": 789}, 0, "uncertain"),
    ({"success": True, "message_id": True}, 0, "uncertain"),
    ({"success": True, "message_id": 789}, 1, "uncertain"),
    ({"error": "private error"}, 1, "uncertain"),
])
def test_claim_before_send_receipts_and_never_resend(h, payload, code, state, monkeypatch):
    event(h)
    calls = []
    monkeypatch.setenv("HERMES_SESSION_ID", "must-not-inherit")
    monkeypatch.setenv("HERMES_CRON_ID", "must-not-inherit")
    def runner(argv, **kwargs):
        calls.append(argv)
        box = lab.read_json(auto.root(h.campaign) / "outbox.json")
        assert next(iter(box["events"].values()))["state"] == "claimed"
        assert argv[1:5] == ["send", "--to", "telegram:-123:456", "--json"]
        assert set(kwargs["env"]) == {"PATH", "HOME", "HERMES_HOME"}
        assert not kwargs.get("shell")
        return SimpleNamespace(returncode=code, stdout=json.dumps(payload))
    with lab.campaign_lock(h.campaign):
        box = delivery(h, runner)  # Separate outbox lock works while campaign busy.
    assert next(iter(box["events"].values()))["state"] == state
    delivery(h, runner)
    assert len(calls) == 1
    assert "private error" not in (auto.root(h.campaign) / "outbox.json").read_text()


def test_accept_then_response_lost_is_uncertain(h):
    event(h)
    accepted = []
    def runner(*args, **kwargs):
        accepted.append(True)
        raise TimeoutError("synthetic accepted but response lost")
    assert next(iter(delivery(h, runner)["events"].values()))["state"] == "uncertain"
    delivery(h, runner)
    assert len(accepted) == 1
    assert len(auto.status(h.campaign)["delivery_uncertain"]) == 1


def test_process_death_after_claim_never_replayed(h):
    event(h)
    def die(*args, **kwargs):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        delivery(h, die)
    def forbidden(*args, **kwargs):
        pytest.fail("Claimed event must not replay")
    box = delivery(h, forbidden)
    assert next(iter(box["events"].values()))["state"] == "claimed"
    assert auto.status(h.campaign)["delivery_uncertain"]


def test_changing_disk_samples_are_one_blocker(h):
    lab.durable_json(h.campaign / "manifest.json", {**h.manifest(), "paused": True}, replace=True)
    for available in (9, 8, 7):
        lab.durable_json(h.campaign / "disk-alert.json",
            {"kind": "lab_disk_paused", "available_blocks": available}, replace=True)
        notes.scan(h.campaign)
    kinds = [row["kind"] for row in notes.scan(h.campaign)["events"].values()]
    assert kinds.count("blocker") == 1


@pytest.mark.parametrize("target", ["telegram", "telegram:123", "telegram:123:456;false", "discord:123:456"])
def test_explicit_target_required(h, target):
    with pytest.raises(ValueError):
        notes.validate_delivery(Path("/usr/bin/false"), target, h.root, h.root)


def test_later_live_blocker_not_suppressed_by_old_failed_report(h):
    event(h, outcome="error")
    original = notes.scan(h.campaign)
    current = h.manifest()
    lab.durable_json(h.campaign / "manifest.json", {**current,
                     "pending": "different-new-attempt", "paused": True}, replace=True)
    lab.durable_json(h.campaign / "disk-alert.json", {"kind": "lab_disk_paused",
                     "reason": "available_space_below_10_percent"})
    updated = notes.scan(h.campaign)
    new = set(updated["events"]) - set(original["events"])
    assert len(new) == 1
    assert updated["events"][new.pop()]["kind"] == "blocker"


def test_default_init_and_scan_never_send(h, monkeypatch):
    monkeypatch.setattr(notes.subprocess, "run", lambda *a, **k: pytest.fail("unexpected send"))
    auto.initialize(h.campaign)
    notes.scan(h.campaign)


def test_real_subprocess_fake_cli(h, monkeypatch):
    event(h)
    executable = Path("/usr/bin/true")
    script = h.root / "fake-hermes-cli.py"
    script.write_text('''import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
assert set(os.environ) <= {"PATH", "HOME", "HERMES_HOME", "LC_CTYPE"}
assert sys.argv[1:5] == ["send", "--to", "telegram:-123:456", "--json"]
with (home / "fake-invocations").open("a") as stream:
    stream.write("synthetic send only\\n")
print(json.dumps({"success": True, "message_id": 54321}))
''')
    home = h.root / "fake-messaging-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_SESSION_ID", "origin-must-not-pass")
    def runner(argv, **kwargs):
        # Test-only fake CLI process; Python reads the fixture on noexec tempfs.
        return subprocess.run([sys.executable, str(script), *argv[1:]], **kwargs)
    for _ in range(2):
        box = notes.deliver(h.campaign, executable=executable, target="telegram:-123:456",
                            home=home, hermes_home=home, runner=runner)
        row = next(iter(box["events"].values()))
        assert row["state"] == "sent" and row["message_id"] == "54321"
    assert (home / "fake-invocations").read_text() == "synthetic send only\n"


def test_report_hash_drift_fails_closed(h):
    event(h)
    (h.campaign / "attempts/synthetic-attempt/report.json").write_text("{}")
    with pytest.raises(ValueError, match="report drift"):
        notes.scan(h.campaign)
