"""Quiet durable local outbox, with opt-in public Hermes CLI dispatch."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable

from scripts import lab_experiment as lab
from scripts.lab_automation import optional, root, short_lock


def discover(campaign: Path) -> dict[str, dict]:
    manifest = lab.read_json(campaign / "manifest.json")
    events: dict[str, dict] = {}
    identity = lab.sha(str(campaign.resolve()))

    def add(kind: str, source: object) -> None:
        key = lab.sha([identity, source, kind])
        events[key] = {"kind": kind, "state": "pending",
                       "text": f"Private lab {kind}. Event {key}. Inspect local campaign status."}

    for pointer in manifest["reports"]:
        path = campaign / "attempts" / pointer["attempt_id"] / "report.json"
        if lab.file_sha(path) != pointer["sha256"]:
            raise ValueError("Authoritative report drift")
        report = lab.read_json(path)
        outcome = report["outcome"]
        if outcome == "accepted":
            add("promotion", [pointer["attempt_id"], pointer["sha256"]])
        elif outcome in ("error", "interrupted"):
            add("operational_failure", [pointer["attempt_id"], pointer["sha256"]])
    # Identity deliberately excludes changing disk samples: one ongoing blocker is quiet.
    latest = (lab.read_json(campaign / "attempts" / manifest["reports"][-1]["attempt_id"] / "report.json")
              if manifest["reports"] else {})
    failure = optional(root(campaign) / "failure.json")
    reported_failure = (latest.get("outcome") in ("error", "interrupted")
                        and (manifest["pending"] is None or latest.get("attempt_id") == manifest["pending"])
                        and (not failure or failure.get("kind") == "operational_failure"))
    active_alert = False
    for filename in ("disk-alert.json", "recovery-alert.json"):
        alert = optional(campaign / filename)
        if alert and manifest["paused"] and not reported_failure:
            active_alert = True
            add("blocker", [filename, alert.get("attempt_id", manifest["pending"]),
                            alert.get("reason", alert.get("kind")), failure.get("observed_at")])
    if manifest["paused"] and not active_alert and not reported_failure:
        add("safety_pause", [manifest["pending"], manifest.get("pause_source")])
    if failure and not reported_failure and not active_alert:
        add("controller_failure", failure)
    retention = optional(root(campaign) / "retention.json")
    if retention.get("blocked") and not failure:
        add("retention_blocker", retention["blocked"])
    return events


def scan(campaign: Path) -> dict:
    """Reconstruct from authority after crashes; independent of the campaign flock."""
    with short_lock(campaign, "outbox"):
        path = root(campaign) / "outbox.json"
        box = optional(path) or {"events": {}}
        for key, event in discover(campaign).items():
            box["events"].setdefault(key, event)
        # An interrupted sender is ambiguous, never pending again.
        lab.durable_json(path, box, replace=True)
        return box


def validate_delivery(executable: Path, target: str, home: Path, hermes_home: Path) -> None:
    if not re.fullmatch(r"telegram:-?[0-9]+:[0-9]+", target):
        raise ValueError("Explicit Telegram chat and topic required")
    if not all(p.is_absolute() for p in (executable, home, hermes_home)):
        raise ValueError("Explicit absolute executable and isolated homes required")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Trusted executable unavailable")
    if executable.stat().st_mode & 0o022:
        raise ValueError("Executable writable by group or others")
    if not home.is_dir() or not hermes_home.is_dir():
        raise ValueError("Operator-provisioned messaging homes required")


def deliver(campaign: Path, *, executable: Path, target: str, home: Path, hermes_home: Path,
            runner: Callable = subprocess.run) -> dict:
    """At most one invocation per event; transport failures sacrifice delivery, not safety.

    The operator supplies messaging-only homes; never modifies the lab model env.
    No credentials or raw CLI output are read into reports.
    """
    validate_delivery(executable, target, home, hermes_home)
    scan(campaign)
    path = root(campaign) / "outbox.json"
    with short_lock(campaign, "outbox"):
        keys = list(lab.read_json(path)["events"])
    for key in keys:
        with short_lock(campaign, "outbox"):
            box = lab.read_json(path)
            event = box["events"][key]
            if event["state"] != "pending":
                continue
            event.update(state="claimed", target=target)
            lab.durable_json(path, box, replace=True)  # Before any possible send.
        receipt: dict = {"state": "uncertain"}
        try:
            result = runner([str(executable), "send", "--to", target, "--json", event["text"]],
                            env={"PATH": "/usr/bin:/bin", "HOME": str(home),
                                 "HERMES_HOME": str(hermes_home)},
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
            payload = json.loads(result.stdout)
            message_id = payload.get("message_id")
            if (result.returncode == 0 and payload.get("success") is True
                    and not payload.get("error") and not payload.get("skipped")
                    and isinstance(message_id, (str, int)) and not isinstance(message_id, bool)
                    and re.fullmatch(r"[0-9]+", str(message_id))):
                receipt = {"state": "sent", "message_id": str(message_id)}
        except Exception:
            pass  # Any unknown outcome remains uncertain, without automatic resend.
        with short_lock(campaign, "outbox"):
            box = lab.read_json(path)
            box["events"][key].update(receipt)
            lab.durable_json(path, box, replace=True)
    return lab.read_json(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "deliver"))
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--messaging-home", type=Path)
    parser.add_argument("--messaging-hermes-home", type=Path)
    args = parser.parse_args()
    campaign = args.campaign.resolve(strict=True)
    if args.command == "scan":
        result = scan(campaign)
    else:
        if not all((args.executable, args.target, args.messaging_home, args.messaging_hermes_home)):
            parser.error("deliver requires executable, target and both isolated messaging homes")
        result = deliver(campaign, executable=args.executable, target=args.target,
                         home=args.messaging_home, hermes_home=args.messaging_hermes_home)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
