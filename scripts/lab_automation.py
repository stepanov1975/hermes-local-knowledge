"""One-shot hourly admission. No scheduler, catch-up queue or provider retries."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import json
import math
import os
from pathlib import Path
import time
from typing import Callable, Iterator

from scripts import lab_disk_guard as disk
from scripts import lab_experiment as lab


def root(campaign: Path) -> Path:
    return campaign / "automation"


def initialize(campaign: Path) -> dict:
    """Explicit local initialization only; never launches a worker or sends."""
    lab.read_json(campaign / "manifest.json")
    directory = root(campaign)
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    with short_lock(campaign, "control"):
        path = directory / "control.json"
        if not path.exists():
            lab.durable_json(path, {"manual_paused": False})
    lab.fsync_directory(campaign)
    return status(campaign)


@contextmanager
def short_lock(campaign: Path, name: str) -> Iterator[None]:
    fd = os.open(root(campaign) / (name + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def manual_pause(campaign: Path, paused: bool) -> dict:
    with short_lock(campaign, "control"):
        lab.durable_json(root(campaign) / "control.json", {"manual_paused": paused}, replace=True)
    return status(campaign)


def optional(path: Path) -> dict:
    return lab.read_json(path) if path.exists() else {}


def status(campaign: Path) -> dict:
    """Read only: no initialization, locks, Docker inspection or message dispatch."""
    manifest = lab.read_json(campaign / "manifest.json")
    outbox = optional(root(campaign) / "outbox.json")
    return {"initialized": (root(campaign) / "control.json").exists(),
            "manual_paused": optional(root(campaign) / "control.json").get("manual_paused", False),
            "safety_paused": manifest["paused"], "pending": manifest["pending"],
            "admission": optional(root(campaign) / "admission.json"),
            "failure": optional(root(campaign) / "failure.json"),
            "retention": optional(root(campaign) / "retention.json"),
            "delivery_uncertain": [key for key, event in outbox.get("events", {}).items()
                                   if event["state"] in ("claimed", "uncertain")]}


def check_space(campaign: Path) -> None:
    def pause(alert: dict) -> None:
        current = lab.read_json(campaign / "manifest.json")
        current.pop("pause_source", None)
        lab.durable_json(campaign / "manifest.json", {**current, "paused": True}, replace=True)
        lab.durable_json(campaign / "disk-alert.json", alert, replace=True)
    # Admission/maintenance preflight only. The existing experiment owns its
    # continuously joined monitor; never place two competing monitors around it.
    with disk.DiskGuard(campaign, pause):
        pass


def maintain(campaign: Path, now: float) -> dict:
    """Called with the campaign flock; protect all work until recovery settles it."""
    from scripts.lab_retention import cleanup_finished
    manifest = lab.read_json(campaign / "manifest.json")
    if manifest["pending"] is not None:
        return {"state": "pending_protected"}
    check_space(campaign)
    result = cleanup_finished(campaign, now=now)
    lab.durable_json(root(campaign) / "retention.json", result, replace=True)
    if result["blocked"]:
        lab.durable_json(root(campaign) / "failure.json",
                         {"kind": "retention_blocked", "blocked": result["blocked"]}, replace=True)
    return result


def acknowledge_blockers(campaign: Path) -> dict:
    """Explicit operator acknowledgment after repair, never recovery or a launch."""
    with lab.campaign_lock(campaign):
        current = lab.read_json(campaign / "manifest.json")
        lab.require(current["pending"] is None, "Reconcile pending work through explicit recovery first")
        check_space(campaign)
        config = lab.read_json(campaign / "config.json")
        lab.require(lab.sha(config) == current["config_sha256"], "Campaign configuration drift")
        lab.validate_config(config)
        failure = root(campaign) / "failure.json"
        if failure.exists():
            saved = lab.read_json(failure)
            archive = failure.with_name("failure-acknowledged-" + lab.sha(saved) + ".json")
            if not archive.exists():
                lab.durable_json(archive, saved)
            lab.require(lab.read_json(archive) == saved, "Failure acknowledgment readback failed")
            failure.unlink()
            lab.fsync_directory(failure.parent)
        updated = {**current, "paused": False}
        updated.pop("pause_source", None)
        lab.durable_json(campaign / "manifest.json", updated, replace=True)
        lab.require(lab.read_json(campaign / "manifest.json") == updated, "Resume readback failed")
    return manual_pause(campaign, False)


def tick(campaign: Path, *, now: float | None = None,
         action: Callable[..., dict] = lab.run_under_existing_lock) -> dict:
    campaign = campaign.resolve(strict=True)
    if not status(campaign)["initialized"]:
        raise ValueError("Explicit automation init required")
    stamp = time.time() if now is None else now
    if not math.isfinite(stamp) or stamp < 0:
        raise ValueError("Finite nonnegative UTC timestamp required")
    try:
        return _tick(campaign, stamp, action, time.time if now is None else lambda: stamp)
    finally:
        from scripts.lab_notifications import scan
        scan(campaign)  # Busy/paused ticks can still observe an in-flight disk alert.


def _tick(campaign: Path, stamp: float, action: Callable[..., dict], clock: Callable[[], float]) -> dict:
    # Catch ONLY acquisition errors, never BlockingIOError raised during execution.
    with ExitStack() as stack:
        try:
            stack.enter_context(lab.campaign_lock(campaign))
        except BlockingIOError:
            return {"state": "busy"}
        manifest = lab.read_json(campaign / "manifest.json")
        if not manifest["paused"]:
            try:
                maintain(campaign, stamp)
            except Exception:
                lab.durable_json(root(campaign) / "failure.json",
                                 {"kind": "maintenance_failure", "observed_at": stamp}, replace=True)
                raise
        with short_lock(campaign, "control"):
            manifest = lab.read_json(campaign / "manifest.json")
            control = lab.read_json(root(campaign) / "control.json")
            if control["manual_paused"] or manifest["paused"] or optional(root(campaign) / "failure.json"):
                return {"state": "paused"}
            hour = math.floor(stamp / 3600)
            prior = optional(root(campaign) / "admission.json")
            if prior and hour <= prior["hour"]:
                return {"state": "hour_skipped"}
            recovery = manifest["pending"] is not None
            reservation = {"hour": hour, "action": "recover" if recovery else "run"}
            lab.durable_json(root(campaign) / "admission.json", reservation, replace=True)
        try:
            result = action(campaign, recover_only=recovery)
            maintain(campaign, clock())
        except Exception:
            # Structural-only text: provider/candidate error strings are private.
            lab.durable_json(root(campaign) / "failure.json",
                             {"kind": "operational_failure", "hour": hour, "observed_at": clock(),
                              "attempt_id": lab.read_json(campaign / "manifest.json")["pending"]}, replace=True)
            raise
        return {"state": "completed", "action": reservation["action"], "result": result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "tick", "pause", "resume", "status"))
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--acknowledge-blockers", action="store_true",
                        help="resume only: after repair, preserve failure receipt and clear safety/operational pauses; pending work must already be reconciled")
    args = parser.parse_args()
    if args.acknowledge_blockers and args.command != "resume":
        parser.error("--acknowledge-blockers is only valid with resume")
    campaign = args.campaign.resolve(strict=True)
    if args.command == "init":
        result = initialize(campaign)
    elif args.command == "resume" and args.acknowledge_blockers:
        result = acknowledge_blockers(campaign)
    elif args.command in ("pause", "resume"):
        result = manual_pause(campaign, args.command == "pause")
    elif args.command == "tick":
        result = tick(campaign)
        if "result" in result:
            result["result"] = {key: result["result"][key] for key in ("attempt_id", "outcome", "revision")
                                if key in result["result"]}
    else:
        result = status(campaign)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
