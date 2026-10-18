"""Post-finalization disposal of enumerated lab paths, never execution recovery.

``cleanup_finished`` requires the campaign flock already held. The standalone
``cleanup_campaign`` wrapper takes that lock; neither entry point launches work.
"""
from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
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
# Retention includes complete controller output streams (64 MiB per command),
# evaluator indexes and full-check evidence, not only 8 MiB source-workspace files.
# Oversized attempts are retained for operator inspection, never partially planned.
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 50000
MAX_DEPTH = 64
CHUNK_BYTES = 64 * 1024
# Only these exact attempt-root slots belong to retention, outside candidate
# exports. Staging names are fixed so even repeated interrupted writes stay finite.
BOOKKEEPING_NAMES = frozenset({"retention-plan.json", "retention-result.json",
                               ".retention-plan.json.pending", ".retention-result.json.pending"})


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


class _Budget:
    def __init__(self, *, bookkeeping: bool = False) -> None:
        self.entries = 0
        self.bytes = 0
        self.byte_limit = MAX_FILE_BYTES if bookkeeping else MAX_TREE_BYTES

    def entry(self, depth: int) -> None:
        self.entries += 1
        _require(self.entries <= MAX_ENTRIES, "retention_entry_budget_exceeded")
        _require(depth <= MAX_DEPTH, "retention_depth_budget_exceeded")

    def content(self, size: int) -> None:
        _require(0 <= size <= MAX_FILE_BYTES, "retention_file_budget_exceeded")
        self.bytes += size
        _require(self.bytes <= self.byte_limit, "retention_byte_budget_exceeded")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    # Exclude atime and nlink: hashing and deletion of another ordinary hardlink
    # legitimately change them. Content/identity changes during a read do not.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _chunks(path: Path, budget: _Budget) -> Iterator[bytes]:
    _plain(path)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode), "nonregular_record")
    budget.content(before.st_size)  # Reject logical sparse size BEFORE open.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _require(_identity(os.fstat(fd)) == _identity(before), "retention_read_drift")
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(CHUNK_BYTES, remaining))
            _require(bool(chunk), "retention_read_drift")
            remaining -= len(chunk)
            _require(_identity(os.fstat(fd)) == _identity(before), "retention_read_drift")
            yield chunk
        _plain(path)
        _require(_identity(os.fstat(fd)) == _identity(before)
                 and _identity(path.lstat()) == _identity(before), "retention_read_drift")
    finally:
        os.close(fd)


def _digest(path: Path, budget: _Budget | None = None) -> str:
    digest = hashlib.sha256()
    for chunk in _chunks(path, budget if budget is not None else _Budget()):
        digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, *, bookkeeping: bool = False) -> dict:
    value = json.loads(b"".join(_chunks(path, _Budget(bookkeeping=bookkeeping))))
    _require(isinstance(value, dict), "nonobject_record")
    return value


def _children(path: Path, budget: _Budget, depth: int,
              bookkeeping: frozenset[Path] = frozenset()) -> Iterator[Path]:
    _plain(path)
    before = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _require(_identity(os.fstat(fd)) == _identity(before), "retention_read_drift")
        with os.scandir(fd) as stream:
            for child in stream:
                if path / child.name not in bookkeeping:
                    budget.entry(depth)
                yield path / child.name
        _plain(path)
        _require(_identity(os.fstat(fd)) == _identity(before)
                 and _identity(path.lstat()) == _identity(before), "retention_read_drift")
    finally:
        os.close(fd)


def _walk(path: Path, budget: _Budget, depth: int = 1,
          bookkeeping: frozenset[Path] = frozenset()) -> Iterator[Path]:
    for child in _children(path, budget, depth, bookkeeping):
        yield child
        info = child.lstat()
        if stat.S_ISDIR(info.st_mode):
            yield from _walk(child, budget, depth + 1, bookkeeping)


def _preflight(attempt: Path) -> None:
    # Metadata only: bound the whole attempt, including protected evaluator output,
    # before controller receipt discovery or any content read. Repeat on restart.
    budget = _Budget()
    bookkeeping = frozenset(attempt / name for name in BOOKKEEPING_NAMES)
    for path in _walk(attempt, budget, bookkeeping=bookkeeping):
        info = path.lstat()
        if path in bookkeeping:
            _require(stat.S_ISREG(info.st_mode), "nonregular_record")
            _Budget(bookkeeping=True).content(info.st_size)
            continue
        if stat.S_ISREG(info.st_mode):
            budget.content(info.st_size)
        else:
            _require(stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode),
                     "foreign_or_special_file")


def _generations(attempt: Path) -> list[Path]:
    return [p for p in _children(attempt, _Budget(), 1,
                                frozenset(attempt / name for name in BOOKKEEPING_NAMES))
            if re.fullmatch(r"generation-[0-9]+", p.name)]


def _sessions(attempt: Path) -> list[Path]:
    return [attempt / "implementation", attempt / "review",
            *(p / "review" for p in _generations(attempt))]


def _save(path: Path, value: dict, *, replace: bool = False) -> None:
    data = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    _Budget(bookkeeping=True).content(len(data))  # Reject before durable publication.
    temp = path.with_name("." + path.name + ".pending")
    _plain(temp, missing=True)
    if temp.exists():
        _require(stat.S_ISREG(temp.lstat().st_mode), "nonregular_record")
        # Never truncate a possible hardlink to evidence. This slot is not authority.
        temp.unlink()
        recovery.sync_directory(temp.parent)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)
        recovery.sync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()
            recovery.sync_directory(temp.parent)
    _require(_json(path, bookkeeping=True) == value, "receipt_readback_failed")


def _roots(attempt: Path) -> list[str]:
    generations = _generations(attempt)
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
    discovery = _Budget()
    for session in _sessions(attempt):
        _plain(session, missing=True)
        private = session / "private"
        _plain(private, missing=True)
        if private.exists():
            paths.extend(p for p in _children(private, discovery, 1) if re.fullmatch(
                r"(?:request-[0-9]+\.json|wire-[0-9]+\.json|response-[0-9]+\.json|tool-[0-9]+-[0-9]+\.(?:stdout|stderr))", p.name))
    for generation in [attempt, *_generations(attempt)]:
        for role in ("evaluation-incumbent", "evaluation-candidate", "full-checks"):
            root = generation / role
            _plain(root, missing=True)
            if root.exists():
                paths.extend(p for p in _children(root, discovery, 1) if re.fullmatch(
                    r"(?:(?:transport|trusted-tests|full_pytest|ruff|mypy)\.(?:stdout|stderr)|private-cases\.jsonl|warmups\.json)", p.name))
        proof = generation / "review-proof"
        _plain(proof, missing=True)
        if proof.exists():
            paths.extend(p for p in _children(proof, discovery, 1) if re.fullmatch(
                r"(?:full_pytest|ruff|mypy)\.(?:stdout|stderr)", p.name))
    return sorted(p.relative_to(attempt).as_posix() for p in paths)


def _entry(path: Path, budget: _Budget | None = None) -> dict:
    info = path.lstat()
    result: dict[str, Any] = {"device": info.st_dev, "inode": info.st_ino}
    if stat.S_ISLNK(info.st_mode):
        return {**result, "kind": "link", "target": os.readlink(path)}
    if stat.S_ISDIR(info.st_mode):
        return {**result, "kind": "directory"}
    # Disposal unlinks only an enumerated name; other hardlinks retain their
    # inode and bytes. Link counts also change legitimately during partial disposal.
    _require(stat.S_ISREG(info.st_mode), "foreign_or_special_file")
    return {**result, "kind": "file", "sha256": _digest(path, budget)}


def _inventory(attempt: Path, names: list[str]) -> dict[str, dict]:
    result = {}
    budget = _Budget()
    for name in names:
        root = attempt / name
        _plain(root, missing=True)
        if not root.exists():
            continue
        budget.entry(len(Path(name).parts))
        result[name] = _entry(root, budget)
        if result[name]["kind"] == "directory":
            for path in _walk(root, budget, len(Path(name).parts) + 1):
                # Links inside an owned sandbox are unlinked, never traversed.
                result[path.relative_to(attempt).as_posix()] = _entry(path, budget)
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
    budget, children = _Budget(), _Budget()
    for name, row in entries.items():
        budget.entry(len(Path(name).parts))
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
            actual = _entry(path, budget)
        _require(actual == row, "planned_path_drift")
        if row["kind"] == "directory":
            _require(all(p.relative_to(attempt).as_posix() in entries for p in _children(
                         path, children, len(Path(name).parts) + 1)),
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


def settlement_evidence(attempt: Path) -> dict[str, str]:
    """Bind recovery to exact controller receipts, not a reusable success flag."""
    discovery, reads = _Budget(), _Budget()
    paths = [attempt / "attempt.json", attempt / "checkpoint.json"]
    paths.extend(_children(attempt / "resources", discovery, 1))
    for session in _sessions(attempt):
        private = session / "private"
        _plain(private, missing=True)
        if private.exists():
            paths.extend(p for p in _children(private, discovery, 1) if p.suffix == ".json")
    return {p.relative_to(attempt).as_posix(): _digest(p, reads) for p in sorted(paths)}


def _reconciled(attempt: Path, manifest: dict, pointer: dict, report: dict) -> dict | None:
    flag = report.get("reconciliation_required", False)
    if flag is False:
        return None
    _require(flag is True and report.get("outcome") != "accepted", "reconciliation_required")
    path = attempt / "recovery-report.json"
    _require(pointer.get("recovery_sha256") == _digest(path), "recovery_receipt_drift")
    receipt = _json(path)
    _require(receipt.get("schema") == "lab-finalized-recovery/v1"
             and receipt.get("state") == "finalized_report_settled"
             and receipt.get("attempt_id") == pointer["attempt_id"]
             and receipt.get("report_sha256") == pointer["sha256"]
             and receipt.get("config_sha256") == manifest["config_sha256"]
             and receipt.get("model_completion") in {"settled", "incomplete"}
             and isinstance(receipt.get("resources"), list)
             and bool(receipt["resources"])
             and all(r.get("state") == "absent" for r in receipt["resources"]),
             "recovery_receipt_invalid")
    return receipt


def _cleanup_attempt(campaign: Path, manifest: dict, pointer: dict, now: float) -> dict:
    from scripts.lab_experiment import sha
    attempt_id = pointer["attempt_id"]
    _require(isinstance(attempt_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", attempt_id) is not None,
             "attempt_identity_invalid")
    _require(manifest.get("pending") != attempt_id, "attempt_pending")
    attempt = campaign / "attempts" / attempt_id
    _plain(attempt)
    _preflight(attempt)
    report_path = attempt / "report.json"
    _require(_digest(report_path) == pointer["sha256"], "report_drift")
    report = _json(report_path)
    _require(report.get("attempt_id") == attempt_id, "report_identity_mismatch")
    reconciled = _reconciled(attempt, manifest, pointer, report)
    _require(report.get("outcome") in {"accepted", "rejected", "no_change", "error", "interrupted"},
             "outcome_unknown")
    config = _json(campaign / "config.json")
    _require(sha(config) == manifest["config_sha256"], "configuration_drift")
    repo = campaign / "repo.git"
    _plain(repo)
    _plain(attempt / "resources")
    has_receipt = False
    for resource_record in _children(attempt / "resources", _Budget(), 1):
        _plain(resource_record)
        has_receipt |= resource_record.suffix == ".json"
        if resource_record.suffix == ".observed":
            _require(resource_record.with_suffix(".json").is_file(), "resource_receipt_missing")
    _require(has_receipt, "resource_receipts_missing")
    receipt_path = attempt / "retention-plan.json"
    resources = recovery.inspect_resources(attempt)
    _require(all(r.get("state") in {"absent", "stopped"} for r in resources), "resource_not_quiescent")
    if reconciled is not None:
        _require(resources == reconciled["resources"], "recovery_settlement_drift")
    if receipt_path.exists() or receipt_path.is_symlink():
        plan = _json(receipt_path, bookkeeping=True)
        _require(plan.get("schema") == SCHEMA and plan["report_sha256"] == pointer["sha256"]
                 and plan["attempt_id"] == attempt_id, "retention_plan_drift")
        if reconciled is not None:
            _require(plan.get("recovery_sha256") == pointer["recovery_sha256"],
                     "retention_plan_drift")
    else:
        # Finalized, non-pending work may have a request saved before dispatch
        # (or an unfinished local tool intent). With resources quiescent it need
        # not have succeeded to be disposable. Unanswered wires still raise.
        completion = recovery.session_completion(attempt)
        _require(completion in {"settled", "incomplete"}, "completion_uncertain")
        if reconciled is not None:
            _require(completion == reconciled["model_completion"]
                     and settlement_evidence(attempt) == reconciled.get("evidence"),
                     "recovery_settlement_drift")
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
        discovery, reads = _Budget(), _Budget()
        bookkeeping = frozenset(attempt / name for name in BOOKKEEPING_NAMES)
        for path in _walk(attempt, discovery, bookkeeping=bookkeeping):
            name = path.relative_to(attempt).as_posix()
            if path not in bookkeeping and name not in disposable and name not in raw and not path.is_dir():
                protected_hashes[name] = _digest(path, reads)
        revisions = {report["revision"], manifest["incumbent"], config["initial_incumbent"]}
        for path in _children(attempt, _Budget(), 1, bookkeeping):
            if re.fullmatch(r"report\.pre-recovery-.*\.json", path.name):
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
                "completion": completion, "resources": resources}
        if reconciled is not None:
            plan["recovery_sha256"] = pointer["recovery_sha256"]
        _save(receipt_path, plan)
    _require(type(plan["finalized_at"]) in {int, float} and math.isfinite(plan["finalized_at"])
             and plan["raw_delete_after"] == plan["finalized_at"] + RAW_RETENTION_SECONDS,
             "retention_clock_invalid")
    for revision in plan["refs"]:
        _anchor(repo, revision)
    reads = _Budget()
    _require(len(plan["protected"]) <= MAX_ENTRIES, "retention_entry_budget_exceeded")
    for name, digest in plan["protected"].items():
        _require(_digest(attempt / name, reads) == digest, "structural_evidence_drift")
    expired = now >= plan["raw_delete_after"]
    if reconciled is not None:
        evidence = dict(reconciled["evidence"])
        if expired:
            # A durable retention plan, not a missing response alone, authorizes
            # raw removal and restart after a partially disposed raw pair.
            for name in plan["raw"]:
                if not (attempt / name).exists():
                    evidence.pop(name, None)
        _require(settlement_evidence(attempt) == evidence, "recovery_settlement_drift")
    # A saved plan authorizes removal of only its original receipt set. Newly
    # arrived request/wire evidence must block admission even on resumed cleanup.
    _require(set(_raw_paths(attempt)) <= set(plan["raw"]), "unplanned_raw_evidence")
    _validate_remaining(attempt, plan["raw"])
    if not expired:
        # Before expiry even a missing raw file is unexplained drift.
        for name, row in plan["raw"].items():
            _require((attempt / name).exists(), "raw_evidence_drift")
    _quiescent(attempt)
    _dispose(attempt, plan["disposable"])
    if expired:
        _dispose(attempt, plan["raw"])
    result = {"schema": SCHEMA, "attempt_id": attempt_id, "report_sha256": pointer["sha256"],
              "plan_sha256": _digest(receipt_path, _Budget(bookkeeping=True)), "disposable_complete": True,
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
