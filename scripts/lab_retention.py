"""Post-finalization disposal of enumerated lab paths, never execution recovery.

``cleanup_finished`` requires the campaign flock already held. The standalone
``cleanup_campaign`` wrapper takes that lock; neither entry point launches work.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any

from scripts import lab_disk_guard as disk
from scripts import lab_recovery as recovery

RAW_RETENTION_SECONDS = 7 * 24 * 60 * 60
SCHEMA = "lab-retention/v1"


class RetentionBlocked(ValueError):
    """Fixed safe reason; retain evidence rather than guessing ownership."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise RetentionBlocked(reason)


def _plain(path: Path, *, missing: bool = False) -> None:
    # Check every ancestor, not just resolve(): resolving would hide substitutions.
    for part in [*reversed(path.parents), path]:
        if not part.exists() and not part.is_symlink():
            _require(missing, "path_missing")
            return
        _require(not part.is_symlink(), "symlink_substitution")


def _digest(path: Path) -> str:
    _plain(path)
    _require(stat.S_ISREG(path.lstat().st_mode), "nonregular_record")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path: Path) -> dict:
    from scripts.lab_experiment import read_json
    _plain(path)
    return read_json(path)


def _save(path: Path, value: dict, *, replace: bool = False) -> None:
    recovery.atomic_json(path, value, replace=replace)
    _require(_json(path) == value, "receipt_readback_failed")


def _roots(attempt: Path) -> list[str]:
    generations = [p for p in attempt.glob("generation-*")
                   if re.fullmatch(r"generation-[0-9]+", p.name)]
    roots = ["candidate.index"]
    for generation in [attempt, *generations]:
        _plain(generation)
        prefix = "" if generation == attempt else generation.name + "/"
        for role in (["implementation", "review"] if generation == attempt else ["review"]):
            roots.extend(prefix + role + "/" + child for child in
                         ("work", "home", "inputs", "scratch", "output", "candidate.index"))
        for role in ("evaluation-incumbent", "evaluation-candidate", "full-checks"):
            roots.extend(prefix + role + "/" + child for child in ("work", "home"))
            # Evaluator output contains native-baseline/coverage/index identity
            # evidence, not merely scratch. Keep the entire output conservatively.
            # trusted-tests/ is the controller's pinned source copy; runner.json
            # records its hashes. Neither is a disposable runner sandbox.
        # inputs.json is a structural identity record, not a disposable input.
        roots.extend(prefix + "full-checks/check-inputs/" + child for child in ("exact-candidate", "tools"))
    return roots


def _raw_paths(attempt: Path) -> list[str]:
    paths: list[Path] = []
    for session in recovery.session_directories(attempt):
        _plain(session, missing=True)
        private = session / "private"
        _plain(private, missing=True)
        if private.exists():
            paths.extend(p for p in private.iterdir() if re.fullmatch(
                r"(?:request-[0-9]+\.json|wire-[0-9]+\.json|response-[0-9]+\.json|tool-[0-9]+-[0-9]+\.(?:stdout|stderr))", p.name))
    for generation in [attempt, *[p for p in attempt.glob("generation-*")
                                if re.fullmatch(r"generation-[0-9]+", p.name)]]:
        for role in ("evaluation-incumbent", "evaluation-candidate", "full-checks"):
            root = generation / role
            _plain(root, missing=True)
            if root.exists():
                paths.extend(p for p in root.iterdir() if re.fullmatch(
                    r"(?:(?:transport|trusted-tests|full_pytest|ruff|mypy)\.(?:stdout|stderr)|private-cases\.jsonl|warmups\.json)", p.name))
        proof = generation / "review-proof"
        _plain(proof, missing=True)
        if proof.exists():
            paths.extend(p for p in proof.iterdir() if re.fullmatch(
                r"(?:full_pytest|ruff|mypy)\.(?:stdout|stderr)", p.name))
    return sorted(p.relative_to(attempt).as_posix() for p in paths)


def _entry(path: Path) -> dict:
    info = path.lstat()
    result: dict[str, Any] = {"device": info.st_dev, "inode": info.st_ino}
    if stat.S_ISLNK(info.st_mode):
        return {**result, "kind": "link", "target": os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):
        return {**result, "kind": "directory"}
    # Disposal unlinks only an enumerated name; other hardlinks retain their
    # inode and bytes. Link counts also change legitimately during partial disposal.
    _require(stat.S_ISREG(info.st_mode), "foreign_or_special_file")
    return {**result, "kind": "file", "sha256": _digest(path)}


def _inventory(attempt: Path, names: list[str]) -> dict[str, dict]:
    result = {}
    for name in names:
        root = attempt / name
        _plain(root, missing=True)
        if not root.exists():
            continue
        result[name] = _entry(root)
        if root.is_dir():
            for directory, dirs, files in os.walk(root, followlinks=False):
                for child in [*dirs, *files]:
                    path = Path(directory) / child
                    # Links inside an owned sandbox are unlinked, never traversed.
                    if path.is_symlink():
                        info = path.lstat()
                        row = {"device": info.st_dev, "inode": info.st_ino,
                               "kind": "link", "target": os.readlink(path)}
                    else:
                        row = _entry(path)
                    result[path.relative_to(attempt).as_posix()] = row
    return result


def _anchor(repo: Path, revision: str) -> str:
    from scripts.lab_experiment import git
    _require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
             "revision_invalid")
    _require(git(repo, "rev-parse", revision + "^{commit}").decode().strip() == revision,
             "revision_unavailable")
    ref = "refs/lab/retained/" + revision
    git(repo, "update-ref", ref, revision)
    _require(git(repo, "rev-parse", ref).decode().strip() == revision, "revision_anchor_failed")
    return ref


def _quiescent(attempt: Path) -> list[dict]:
    rows = recovery.inspect_resources(attempt)
    _require(all(r.get("state") in {"absent", "stopped"} for r in rows), "resource_not_quiescent")
    if any(r["state"] == "stopped" for r in rows):
        recovery.settle_resources(attempt)
    rows = recovery.inspect_resources(attempt)
    _require(all(r.get("state") == "absent" for r in rows), "resource_removal_unconfirmed")
    return rows


def _validate_remaining(attempt: Path, entries: dict[str, dict]) -> None:
    for name, row in entries.items():
        path = attempt / name
        _plain(path.parent, missing=True)
        if not path.exists() and not path.is_symlink():
            continue  # A durable plan authorizes this exact deletion after restart.
        if row["kind"] == "link":
            info = path.lstat()
            actual = {"device": info.st_dev, "inode": info.st_ino, "kind": "link",
                      "target": os.readlink(path)} if path.is_symlink() else {}
        else:
            _plain(path)
            actual = _entry(path)
        _require(actual == row, "planned_path_drift")
        if row["kind"] == "directory":
            _require(all(p.relative_to(attempt).as_posix() in entries for p in path.iterdir()),
                     "unplanned_data")


def _dispose(attempt: Path, entries: dict[str, dict]) -> None:
    _validate_remaining(attempt, entries)
    # Frozen corpus exports retain 0555 directories. Grant only owner-write on
    # verified disposable directories, never chmod files (which may be hardlinked
    # to protected data). Directory plan identity excludes mode, so an interruption
    # after this authorized chmod resumes against the same immutable plan.
    for name, row in entries.items():
        path = attempt / name
        if row["kind"] != "directory" or not path.exists():
            continue
        _plain(path)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            _require((info.st_dev, info.st_ino) == (row["device"], row["inode"]),
                     "planned_path_drift")
            if not info.st_mode & stat.S_IWUSR:
                _require(info.st_uid == os.geteuid(), "directory_owner_mismatch")
                os.fchmod(fd, stat.S_IMODE(info.st_mode) | stat.S_IWUSR)
                os.fsync(fd)
        finally:
            os.close(fd)
    for name in sorted(entries, key=lambda n: (n.count("/"), n), reverse=True):
        path = attempt / name
        _plain(path.parent, missing=True)
        if not path.exists() and not path.is_symlink():
            continue
        if entries[name]["kind"] == "directory":
            path.rmdir()  # Refuse unplanned data, never a recursive wildcard delete.
        else:
            path.unlink()
        recovery.sync_directory(path.parent)
    _require(all(not (attempt / n).exists() and not (attempt / n).is_symlink() for n in entries),
             "disposal_readback_failed")


def _cleanup_attempt(campaign: Path, manifest: dict, pointer: dict, now: float) -> dict:
    from scripts.lab_experiment import sha
    attempt_id = pointer["attempt_id"]
    _require(isinstance(attempt_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", attempt_id) is not None,
             "attempt_identity_invalid")
    _require(manifest.get("pending") != attempt_id, "attempt_pending")
    attempt = campaign / "attempts" / attempt_id
    _plain(attempt)
    report_path = attempt / "report.json"
    _require(_digest(report_path) == pointer["sha256"], "report_drift")
    report = _json(report_path)
    _require(report.get("attempt_id") == attempt_id, "report_identity_mismatch")
    _require(report.get("reconciliation_required", False) is False, "reconciliation_required")
    _require(report.get("outcome") in {"accepted", "rejected", "no_change", "error", "interrupted"},
             "outcome_unknown")
    config = _json(campaign / "config.json")
    _require(sha(config) == manifest["config_sha256"], "configuration_drift")
    repo = campaign / "repo.git"
    _plain(repo)
    _plain(attempt / "resources")
    _require((attempt / "resources").is_dir()
             and any((attempt / "resources").glob("*.json")), "resource_receipts_missing")
    for record in (attempt / "resources").iterdir():
        _plain(record)
        if record.suffix == ".observed":
            _require(record.with_suffix(".json").is_file(), "resource_receipt_missing")
    receipt_path = attempt / "retention-plan.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        existing = _json(receipt_path)
        for name, digest in existing["protected"].items():
            _require(_digest(attempt / name) == digest, "structural_evidence_drift")
    resources = recovery.inspect_resources(attempt)
    _require(all(r.get("state") in {"absent", "stopped"} for r in resources), "resource_not_quiescent")
    if receipt_path.exists() or receipt_path.is_symlink():
        plan = _json(receipt_path)
        _require(plan.get("schema") == SCHEMA and plan["report_sha256"] == pointer["sha256"]
                 and plan["attempt_id"] == attempt_id, "retention_plan_drift")
    else:
        _require(recovery.session_completion(attempt) == "settled", "completion_uncertain")
        roots = _roots(attempt)
        # Never delete a frozen/configured source even if its path was placed
        # inside an otherwise disposable export.
        for key in ("corpus", "frozen", "development", "policy", "check_tools", "repo"):
            if key in config:
                protected = Path(config[key]).resolve()
                _require(not any(protected == attempt / r or attempt / r in protected.parents
                                 for r in roots), "protected_input_overlap")
        disposable = _inventory(attempt, roots)
        raw = _inventory(attempt, _raw_paths(attempt))
        protected_hashes = {}
        for directory, dirs, files in os.walk(attempt, followlinks=False):
            dirs[:] = [d for d in dirs if str((Path(directory) / d).relative_to(attempt)) not in disposable]
            for filename in files:
                path = Path(directory) / filename
                name = path.relative_to(attempt).as_posix()
                if name not in disposable and name not in raw:
                    protected_hashes[name] = _digest(path)
        revisions = {report["revision"], manifest["incumbent"], config["initial_incumbent"]}
        for path in attempt.glob("report.pre-recovery-*.json"):
            revisions.add(_json(path)["revision"])
        for name in ("candidate.json", "attempt.json"):
            if (attempt / name).exists():
                record = _json(attempt / name)
                revisions.update(record[k] for k in ("revision", "previous_incumbent") if k in record)
        refs = {revision: _anchor(repo, revision) for revision in sorted(revisions)}
        usage = {}
        for name in raw:
            if re.fullmatch(r"response-[0-9]+\.json", Path(name).name):
                usage[name] = _json(attempt / name).get("usage", {"available": False})
        resources = _quiescent(attempt)
        plan = {"schema": SCHEMA, "attempt_id": attempt_id, "report_sha256": pointer["sha256"],
                "finalized_at": now, "raw_delete_after": now + RAW_RETENTION_SECONDS,
                "disposable": disposable, "raw": raw, "protected": protected_hashes,
                "refs": refs, "usage": usage, "report_usage": report.get("usage", []),
                "completion": "settled", "resources": resources}
        _save(receipt_path, plan)
    _require(type(plan["finalized_at"]) in {int, float} and math.isfinite(plan["finalized_at"])
             and plan["raw_delete_after"] == plan["finalized_at"] + RAW_RETENTION_SECONDS,
             "retention_clock_invalid")
    for revision in plan["refs"]:
        _anchor(repo, revision)
    for name, digest in plan["protected"].items():
        _require(_digest(attempt / name) == digest, "structural_evidence_drift")
    expired = now >= plan["raw_delete_after"]
    _validate_remaining(attempt, plan["raw"])
    if not expired:
        # Before expiry even a missing raw file is unexplained drift.
        for name, row in plan["raw"].items():
            _require(_entry(attempt / name) == row, "raw_evidence_drift")
    _quiescent(attempt)
    _dispose(attempt, plan["disposable"])
    if expired:
        _dispose(attempt, plan["raw"])
    result = {"schema": SCHEMA, "attempt_id": attempt_id, "report_sha256": pointer["sha256"],
              "plan_sha256": _digest(receipt_path), "disposable_complete": True,
              "raw_complete": expired, "raw_delete_after": plan["raw_delete_after"]}
    _save(attempt / "retention-result.json", result, replace=True)
    return result


def cleanup_finished(campaign: Path, *, now: float) -> dict:
    """Caller holds campaign flock. Return safe blockers, never launch an attempt.

    Unknown/low space is a blocker: the automation owner must pause and alert
    before invoking cleanup. This module does not free protected data under pressure.
    """
    _require(type(now) in {int, float} and math.isfinite(now) and now >= 0, "invalid_now")
    campaign = campaign.absolute()
    _plain(campaign)
    held_fds = recovery.lock_fds()
    _require(bool(held_fds), "campaign_lock_required")
    _plain(campaign / "lock")
    expected_lock = (campaign / "lock").stat()
    held_lock = os.fstat(held_fds[0])
    _require((held_lock.st_dev, held_lock.st_ino) == (expected_lock.st_dev, expected_lock.st_ino),
             "campaign_lock_mismatch")
    space = disk.available_space(campaign)
    _require(space["available_blocks"] * 10 >= space["usable_blocks"], "disk_unsafe")
    manifest = _json(campaign / "manifest.json")
    result: dict[str, Any] = {"cleaned": [], "blocked": []}
    for pointer in manifest["reports"]:
        try:
            result["cleaned"].append(_cleanup_attempt(campaign, manifest, pointer, now))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            reason = str(error) if isinstance(error, (RetentionBlocked, recovery.RecoveryPaused)) else "evidence_unavailable"
            result["blocked"].append({"attempt_id": pointer.get("attempt_id"), "reason": reason})
    return result


def cleanup_campaign(campaign: Path, *, now: float) -> dict:
    """Lock-taking public wrapper; do not call while already holding the flock."""
    from scripts.lab_experiment import campaign_lock
    _plain(campaign.absolute())
    with campaign_lock(campaign):
        return cleanup_finished(campaign, now=now)
