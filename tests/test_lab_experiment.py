"""Synthetic control-plane checks: no model, container, corpus, or natural-win claims."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Callable
import uuid

import pytest

from scripts import lab_experiment as lab


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.root = tmp_path
        self.repo = tmp_path / "source"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "hermes_local_knowledge").mkdir()
        (self.repo / "hermes_local_knowledge" / "rank.py").write_text("VALUE = 1\n")
        (self.repo / "hermes_local_knowledge" / "obsolete.py").write_text("OLD = 1\n")
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "controller.py").write_text("TRUSTED = True\n")
        lab.git(self.repo, "add", ".")
        lab.git(self.repo, "commit", "-m", "synthetic incumbent")
        self.initial = lab.git(self.repo, "rev-parse", "HEAD").decode().strip()
        monkeypatch.setattr(lab, "INITIAL_INCUMBENT", self.initial)
        for name in ("corpus",):
            (tmp_path / name).mkdir()
        for name in ("frozen", "development", "policy"):
            (tmp_path / name).write_text("{}")
        self.config = {"schema": lab.SCHEMA, "repo": str(self.repo), "initial_incumbent": self.initial,
                       "image": "sha256:" + "a" * 64, "composite_id": "synthetic-benchmark"}
        for key in lab.INPUT_KEYS:
            if key in lab.PATH_KEYS:
                self.config[key] = str(tmp_path / key)
            elif key.endswith("sha256"):
                self.config[key] = "b" * 64
        tool_sources = {}
        for number, name in enumerate(sorted(lab.TOOL_FILES)):
            source = tmp_path / ("tool-source-" + str(number))
            source.write_bytes(b"synthetic tool fixture, never executed")
            tool_sources[name] = str(source)
        self.config["check_tools"] = str(tmp_path / "tools")
        self.config["check_tools_sha256"] = lab.freeze_check_tools(tmp_path / "tools", tool_sources)
        self.campaign = tmp_path / "campaign"
        self.identity = {"synthetic": "controller-frozen"}
        monkeypatch.setattr(lab, "controller_identity", lambda: dict(self.identity))
        lab.init_campaign(self.campaign, self.config, loader=self.loader)
        self.calls: list[dict] = []
        self.evaluations: list[dict] = []
        self.change = True
        self.hit_improvement = True
        self.review_approved = True
        self.clear = True
        self.checks_passed = True
        self.revisit_reason = ""
        self.strategy = "synthetic deterministic route adjustment"
        self.mutate: Callable[[Path], Any] | None = None
        self.evaluation_mutate: Callable[[dict], Any] | None = None
        self.review_mutate: Callable[[Path], Any] | None = None
        self.failure: BaseException | None = None

    @staticmethod
    def loader(**kwargs: Any) -> dict:
        return {"synthetic": True}

    def export(self, repo: Path, revision: str, workspace: Path) -> None:
        workspace.mkdir(parents=True)
        for name, (_, oid) in lab.tracked(repo, revision).items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(lab.git(repo, "cat-file", "blob", oid))

    def session(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        assert lab.read_json(self.campaign / "manifest.json")["pending"]
        record = self.campaign / "attempts" / lab.read_json(self.campaign / "manifest.json")["pending"] / "attempt.json"
        assert record.is_file()  # Durable identity exists before any model invocation.
        if self.failure is not None:
            raise self.failure
        workspace = kwargs["attempt"] / "work" / "plugin"
        self.export(kwargs["repo"], kwargs["revision"], workspace)
        if kwargs["role"] == "implementation":
            if self.change:
                (workspace / "hermes_local_knowledge" / "rank.py").write_text("VALUE = 2\n")
            if self.mutate:
                self.mutate(workspace)
            final = {"hypothesis": "Synthetic fixture only", "strategy": self.strategy,
                     "related_attempts": [], "revisit_reason": self.revisit_reason,
                     "changes": ["advisory"], "checks": ["untrusted worker claim"],
                     "lessons": ["Synthetic observations only"], "no_change": not self.change}
        else:
            evidence = lab.read_json(kwargs["evidence"])
            assert evidence["draft_report"]["measured_outcomes"]
            assert "private-cases" not in json.dumps(evidence)
            if self.review_mutate:
                self.review_mutate(workspace)
            final = {"approved": self.review_approved, "shortcuts_checked": True,
                     "compatibility_checked": True, "report_checked": True, "findings": []}
        files = {name: hashlib.sha256(data).hexdigest()
                 for name, data in lab.workspace_files(workspace).items()}
        return {"role": kwargs["role"], "revision": kwargs["revision"], "session_id": uuid.uuid4().hex,
                **lab.MODEL, "workspace": str(workspace), "final": final, "usage": [{"input_tokens": 10}],
                "complete": True, "source_identity": {
                    "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), "files": files}}

    def evaluator(self, **kwargs: Any) -> dict:
        self.evaluations.append(kwargs)
        hits = 3 if kwargs["revision"] != self.initial and self.hit_improvement else 2
        result = {"revision": kwargs["revision"], "image": kwargs["image"],
                  "benchmark_identity": self.config["composite_id"], "integrity": True,
                  "trusted_tests": True, "official_integration": True, "complete": True,
                  "errors": 0, "warm_p95_seconds": 0.01, "scored_cases": 8,
                  "development": {"count": 4, "hit_at_5": 2, "hit_at_1": 1, "origin_hit_at_5": 2},
                  "validation": {"count": 4, "hit_at_5": hits, "hit_at_1": 1, "origin_hit_at_5": 2},
                  "private-cases": "must not propagate"}
        if self.evaluation_mutate:
            self.evaluation_mutate(result)
        return result

    def checks(self, **kwargs: Any) -> dict:
        return {"revision": kwargs["revision"], "image": kwargs["image"], "controller": lab.sha(self.identity),
                "passed": self.checks_passed, "checks": [
                    {"name": name, "passed": self.checks_passed} for name in ("full_pytest", "ruff", "mypy")]}

    def run(self, **kwargs: Any) -> dict:
        return lab.run_experiment(self.campaign, session=self.session, evaluator=self.evaluator,
                                  checks=self.checks, clearance=lambda *_: self.clear,
                                  loader=self.loader, **kwargs)

    def manifest(self) -> dict:
        return lab.read_json(self.campaign / "manifest.json")

    def last_report(self) -> dict:
        return lab.finalized_reports(self.campaign, self.manifest())[-1]


@pytest.mark.parametrize("available,expected", [(9, False), (10, True), (11, True)])
def test_disk_preflight_before_attempt_or_dispatch(h, monkeypatch, available, expected):
    from types import SimpleNamespace
    from scripts import lab_disk_guard as disk
    original = h.manifest()
    sampled = []
    def statvfs(path):
        sampled.append(path)
        # Twenty blocks are reserved: df-usable total is 100, not 120.
        return SimpleNamespace(f_blocks=120, f_bfree=20 + available, f_bavail=available)
    monkeypatch.setattr(disk.os, "statvfs", statvfs)
    if expected:
        assert h.run()["outcome"] == "accepted"
    else:
        with pytest.raises(disk.LabDiskPaused):
            h.run()
        assert h.calls == [] and h.evaluations == []
        assert list((h.campaign / "attempts").iterdir()) == []
        assert h.manifest() == {**original, "paused": True}
        alert = lab.read_json(h.campaign / "disk-alert.json")
        assert alert["available_blocks"] == available and alert["usable_blocks"] == 100
        assert alert["kind"] == "lab_disk_paused" and "Free space" in alert["action"]
    assert sampled == [h.campaign.resolve()]


def test_unknown_disk_preflight_fails_closed(h, monkeypatch):
    from scripts import lab_disk_guard as disk
    def unavailable(path):
        raise OSError("synthetic failure containing private text")
    monkeypatch.setattr(disk.os, "statvfs", unavailable)
    with pytest.raises(disk.LabDiskPaused):
        h.run()
    assert h.calls == [] and h.evaluations == []
    alert = (h.campaign / "disk-alert.json").read_text()
    assert "available_space_unknown" in alert and "private text" not in alert
    assert h.manifest()["paused"] is True


def test_disk_monitor_pauses_while_session_blocked_and_prevents_later_phases(h, monkeypatch):
    import threading
    from scripts import lab_disk_guard as disk
    original = h.manifest()
    trigger, sampled, stopped = threading.Event(), threading.Event(), threading.Event()
    low = [False]
    workers = {"this-attempt": True, "unrelated-campaign": True}
    def space(path):
        assert path == h.campaign.resolve()
        return {"available_blocks": 9 if low[0] else 20, "usable_blocks": 100}
    def monitor(self):
        assert trigger.wait(5)
        self.sample()
        sampled.set()
    monkeypatch.setattr(disk, "available_space", space)
    monkeypatch.setattr(disk.DiskGuard, "_monitor", monitor)
    def stop():
        assert h.manifest()["paused"] is True
        assert lab.read_json(h.campaign / "disk-alert.json")["kind"] == "lab_disk_paused"
        workers["this-attempt"] = False
        stopped.set()
    original_session = h.session
    def session(**kwargs):
        with disk.owned_worker(stop):
            low[0] = True
            trigger.set()
            # Simulate a provider request that has not returned: pause must already be durable.
            assert stopped.wait(5) and sampled.wait(5)
            assert h.manifest()["incumbent"] == original["incumbent"]
            assert h.manifest()["reports"] == original["reports"]
            return original_session(**kwargs)
    with pytest.raises(disk.LabDiskPaused):
        lab.run_experiment(h.campaign, session=session, evaluator=h.evaluator,
                           checks=h.checks, loader=h.loader)
    assert workers == {"this-attempt": False, "unrelated-campaign": True}
    assert len(h.calls) == 1 and h.evaluations == []
    assert h.manifest()["incumbent"] == original["incumbent"] and h.manifest()["paused"]
    assert h.last_report()["outcome"] == "error"
    assert h.last_report()["usage"][0]["usage"] == [{"input_tokens": 10}]
    assert not any(t.name == "lab-disk-guard" for t in threading.enumerate())



def test_disk_monitor_cadence_is_sixty_seconds_without_busy_polling(tmp_path, monkeypatch):
    from scripts import lab_disk_guard as disk
    waits, alerts = [], []
    low = [False]
    monkeypatch.setattr(disk, "available_space", lambda path: {
        "available_blocks": 9 if low[0] else 20, "usable_blocks": 100})
    guard = disk.DiskGuard(tmp_path, alerts.append)
    class Clock:
        def wait(self, seconds):
            waits.append(seconds)
            low[0] = len(waits) == 2
            return False
    monkeypatch.setattr(guard.stopping, "wait", Clock().wait)
    guard._monitor()
    assert waits == [60, 60] and len(alerts) == 1 and guard.failed


def test_low_disk_at_full_checks_cannot_launch_review_or_promote(h, monkeypatch):
    from scripts import lab_disk_guard as disk
    h.run()  # Existing immutable history must survive the later safety pause.
    original = h.manifest()
    original_calls = len(h.calls)
    h.change = False  # Legitimate no-change still reaches full checks.
    low = [False]
    monkeypatch.setattr(disk, "available_space", lambda path: {
        "available_blocks": 9 if low[0] else 20, "usable_blocks": 100})
    def checks(**kwargs):
        result = h.checks(**kwargs)
        low[0] = True
        guard = disk._CURRENT.get()
        assert guard is not None
        guard.sample()
        return result
    with pytest.raises(disk.LabDiskPaused):
        lab.run_experiment(h.campaign, session=h.session, evaluator=h.evaluator,
                           checks=checks, loader=h.loader)
    current = h.manifest()
    assert current["incumbent"] == original["incumbent"] and current["paused"] is True
    assert current["reports"][:-1] == original["reports"]
    assert len(h.calls) == original_calls + 1
    assert h.last_report()["outcome"] == "error"


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def test_accepted_then_next_clean_session_inherits_exact_revision_and_image(h: Harness) -> None:
    report = h.run()
    assert report["outcome"] == "accepted"
    assert h.manifest()["incumbent"] == report["revision"] != h.initial
    assert report["actual_changes"] == [{"path": "hermes_local_knowledge/rank.py", "operation": "modify"}]
    assert report["usage"][0]["usage"] == [{"input_tokens": 10}]
    assert all(c["revision"] == report["revision"] for c in report["checks"])
    first_image = h.calls[0]["image"]
    h.change = False
    next_report = h.run()
    assert next_report["outcome"] == "no_change"
    assert h.calls[2]["revision"] == report["revision"]
    assert all(c["image"] == first_image for c in h.calls)
    history = lab.read_json(h.calls[2]["history"])
    assert history["reports"][0]["revision"] == report["revision"]
    assert history["reports"][0]["measured_outcomes"]["candidate"]["validation"] == report["measured_outcomes"]["candidate"]["validation"]
    assert "private-cases" not in json.dumps(history)
    assert "untrusted worker claim" not in json.dumps(report)
    assert '[remote "origin"]' not in (h.campaign / "repo.git" / "config").read_text()


@pytest.mark.parametrize("gate", ["equal_score", "review", "full_checks", "unknown_targets"])
def test_each_gate_independently_rejects_without_losing_history(h: Harness, gate: str) -> None:
    if gate == "equal_score":
        h.hit_improvement = False
    elif gate == "review":
        h.review_approved = False
    elif gate == "full_checks":
        h.checks_passed = False
    else:
        h.clear = False
    report = h.run()
    assert report["outcome"] == "rejected"
    assert h.manifest()["incumbent"] == h.initial
    assert h.last_report() == report
    assert h.manifest()["pending"] is None


@pytest.mark.parametrize("no_change,changed", [(False, False), (True, True)])
def test_delivery_mismatch_stops_before_gates_and_preserves_evidence(h: Harness, no_change: bool,
                                                                  changed: bool) -> None:
    h.run()  # Preserve an existing successful incumbent and immutable report.
    original = h.manifest()
    h.change = changed
    if changed:
        h.mutate = lambda work: (work / "hermes_local_knowledge/rank.py").write_text("VALUE = 3\n")
    original_session = h.session

    def session(**kwargs: Any) -> dict:
        result = original_session(**kwargs)
        result["final"]["no_change"] = no_change
        result["usage"] = [{"input_tokens": 123, "output_tokens": 12}]
        result["cleanup_complete"] = True
        return result

    h.evaluations.clear()
    h.calls.clear()
    with pytest.raises(ValueError, match="Candidate delivery mismatch"):
        lab.run_experiment(h.campaign, session=session, evaluator=h.evaluator,
                           checks=lambda **kwargs: pytest.fail("must not run full checks"), loader=h.loader)
    report = h.last_report()
    current = h.manifest()
    assert report["outcome"] == "error"
    assert report["delivery_validation"] == {
        "passed": False, "reason": "no_change_imported_edits_mismatch",
        "declared_no_change": no_change, "imported_edits": bool(report["actual_changes"])}
    assert lab.history_projection(report)["delivery_validation"] == report["delivery_validation"]
    assert bool(report["actual_changes"]) is changed
    assert report["usage"][0]["usage"] == [{"input_tokens": 123, "output_tokens": 12}]
    assert report["usage"][0]["cleanup_complete"] is True
    assert report["reconciliation_required"] is False
    assert report["checks"] == [] and report["measured_outcomes"] == {}
    assert h.evaluations == [] and [c["role"] for c in h.calls] == ["implementation"]
    assert current["incumbent"] == original["incumbent"]
    assert current["reports"][:-1] == original["reports"]
    assert current["pending"] is None


@pytest.mark.parametrize("changed", [False, True])
def test_delivery_uses_imported_edits_not_advisory_changes(h: Harness, changed: bool) -> None:
    h.change = changed
    original_session = h.session

    def session(**kwargs: Any) -> dict:
        result = original_session(**kwargs)
        if kwargs["role"] == "implementation":
            result["final"]["changes"] = [] if changed else ["advisory observation, not an edit"]
        return result

    report = lab.run_experiment(h.campaign, session=session, evaluator=h.evaluator,
        checks=h.checks, clearance=lambda *_: True, loader=h.loader)
    assert report["outcome"] == ("accepted" if changed else "no_change")
    assert bool(report["actual_changes"]) is changed
    assert len(h.evaluations) == 2
    assert [c["role"] for c in h.calls] == ["implementation", "review"]


def test_nochange_has_execution_review_report_and_unknown_usage(h: Harness) -> None:
    h.change = False
    report = h.run()
    assert report["outcome"] == "no_change"
    assert len(h.evaluations) == 2
    assert [c["role"] for c in h.calls] == ["implementation", "review"]
    assert report["revision"] == h.initial
    session = {"role": "review", "session_id": "s", "usage": []}
    assert lab.safe_usage([session])[0]["usage_status"] == "unknown"
    assert lab.safe_usage([session])[0]["cleanup_complete"] is None


@pytest.mark.parametrize("kind", ["forbidden", "new_forbidden", "symlink", "directory_symlink", "generated", "hardlink"])
def test_rejects_unsafe_worker_files(h: Harness, kind: str) -> None:
    def mutate(work: Path) -> None:
        if kind == "forbidden":
            (work / "scripts/controller.py").write_text("TAMPER = True")
        elif kind == "new_forbidden":
            (work / "tests").mkdir()
            (work / "tests/fake.py").write_text("pass")
        elif kind == "symlink":
            path = work / "hermes_local_knowledge/rank.py"
            path.unlink()
            path.symlink_to(h.root / "frozen")
        elif kind == "directory_symlink":
            (work / "linked").symlink_to(h.root / "corpus", target_is_directory=True)
        elif kind == "generated":
            (work / "hermes_local_knowledge/index.sqlite").write_text("not permitted")
        else:
            (work / "hermes_local_knowledge/hard.py").hardlink_to(work / "hermes_local_knowledge/rank.py")
    h.mutate = mutate
    with pytest.raises(ValueError):
        h.run()
    assert h.manifest()["incumbent"] == h.initial
    assert h.last_report()["outcome"] == "error"
    assert not h.evaluations


def test_candidate_deletes_safely_and_preserves_noneditable_omitted_files(h: Harness) -> None:
    def mutate(work: Path) -> None:
        (work / "hermes_local_knowledge/obsolete.py").unlink()
        (work / "scripts/controller.py").unlink()  # controller omitted by export, never deleted in candidate
    h.mutate = mutate
    report = h.run()
    tree = lab.tracked(h.campaign / "repo.git", report["revision"])
    assert "hermes_local_knowledge/obsolete.py" not in tree
    assert "scripts/controller.py" in tree
    assert {"path": "hermes_local_knowledge/obsolete.py", "operation": "delete"} in report["actual_changes"]


@pytest.mark.parametrize("field,value", [("revision", "f" * 40), ("image", "sha256:" + "f" * 64),
                                          ("benchmark_identity", "different")])
def test_stale_evaluation_cannot_promote(h: Harness, field: str, value: str) -> None:
    h.evaluation_mutate = lambda result: result.update({field: value})
    with pytest.raises(ValueError, match="Stale evaluation"):
        h.run()
    assert h.last_report()["outcome"] == "error"
    assert h.manifest()["incumbent"] == h.initial


def test_reviewer_edit_invalidates_gates(h: Harness) -> None:
    h.review_mutate = lambda work: (work / "hermes_local_knowledge/rank.py").write_text("CHANGED = 1\n")
    with pytest.raises(ValueError, match="Reviewer source changed"):
        h.run()
    assert h.manifest()["incumbent"] == h.initial


def test_implementation_edit_after_evaluation_invalidates_gates(h: Harness) -> None:
    def mutate(result: dict) -> None:
        workspace = h.calls[0]["attempt"] / "work/plugin/hermes_local_knowledge/rank.py"
        workspace.write_text("AFTER = 1\n")
    h.evaluation_mutate = mutate
    with pytest.raises(ValueError, match="Candidate changed after execution"):
        h.run()
    assert h.manifest()["incumbent"] == h.initial


def test_repeated_failed_strategy_needs_reason_but_does_not_ban_approach(h: Harness) -> None:
    h.hit_improvement = False
    assert h.run()["outcome"] == "rejected"
    with pytest.raises(ValueError, match="Repeated failed strategy"):
        h.run()
    h.revisit_reason = "Synthetic new evidence justifies retry"
    h.hit_improvement = True
    assert h.run()["outcome"] == "accepted"


@pytest.mark.parametrize("error_name", ["AuthenticationError", "QuotaExceeded", "ModelPaused", "RateLimitError"])
def test_auth_quota_pauses_and_prevents_doomed_relaunch(h: Harness, error_name: str) -> None:
    error = type(error_name, (RuntimeError,), {})
    h.failure = error("not stored in history")
    with pytest.raises(error):
        h.run()
    assert h.manifest()["paused"] is True
    assert h.last_report()["error_class"] == error_name
    with pytest.raises(ValueError, match="paused or pending"):
        h.run()
    assert len(h.calls) == 1


def test_interrupted_worker_keeps_pending_and_blocks_relaunch(h: Harness) -> None:
    h.failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        h.run()
    assert h.manifest()["pending"]
    assert not h.manifest()["reports"]
    assert h.manifest()["incumbent"] == h.initial
    with pytest.raises(ValueError, match="paused or pending"):
        h.run()
    assert len(h.calls) == 1


def test_interrupted_publication_exposes_neither_unpublished_incumbent_nor_report(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    durable = lab.durable_json
    def interrupted(path: Path, value: Any, *, replace: bool = False) -> None:
        if path.name == "manifest.json" and value.get("reports"):
            raise KeyboardInterrupt
        durable(path, value, replace=replace)
    monkeypatch.setattr(lab, "durable_json", interrupted)
    with pytest.raises(KeyboardInterrupt):
        h.run()
    manifest = h.manifest()
    assert manifest["incumbent"] == h.initial and not manifest["reports"]
    assert manifest["pending"]
    assert (h.campaign / "attempts" / manifest["pending"] / "report.json").exists()
    assert lab.finalized_reports(h.campaign, manifest) == []
    with pytest.raises(ValueError, match="paused or pending"):
        h.run()


def test_publication_compare_and_swap_and_immutable_reports(h: Harness) -> None:
    old = h.manifest()
    report = h.run()
    with pytest.raises(ValueError, match="compare-and-swap"):
        lab.publish(h.campaign, old, report)
    with pytest.raises(FileExistsError):
        lab.durable_json(h.campaign / "attempts" / report["attempt_id"] / "report.json", {})


def test_report_integrity_and_config_pins_checked_before_launch(h: Harness) -> None:
    h.run()
    report_path = h.campaign / "attempts" / h.manifest()["reports"][0]["attempt_id"] / "report.json"
    report_path.write_text("{}")
    with pytest.raises(ValueError, match="Final report drift"):
        h.run()
    assert len(h.calls) == 2


def test_controller_drift_prevents_candidate_gates(h: Harness) -> None:
    def mutate(work: Path) -> None:
        h.identity["synthetic"] = "changed-controller"
    h.mutate = mutate
    with pytest.raises(ValueError, match="Controller/config evidence became stale"):
        h.run()
    assert not h.evaluations


def test_lock_prevents_concurrent_launch(h: Harness) -> None:
    with lab.campaign_lock(h.campaign), pytest.raises(BlockingIOError):
        h.run()
    assert not h.calls


def test_orphan_attempt_directory_blocks_relaunch(h: Harness) -> None:
    (h.campaign / "attempts/orphan").mkdir()
    with pytest.raises(ValueError, match="Unreconciled"):
        h.run()
    assert not h.calls


def test_unknown_target_clearance_is_absent_by_default_and_exact_bound(h: Harness) -> None:
    binding = {"revision": "synthetic", "both_execution_hash": "synthetic-scores"}
    assert not lab.unknown_target_clearance(h.campaign, binding)
    directory = h.campaign / "clearances"
    directory.mkdir()
    path = directory / (lab.sha(binding) + ".json")
    receipt = {"binding": binding, "symmetric_source_review": True,
               "unknown_targets_cleared": True, "rationale": "Synthetic control only"}
    lab.durable_json(path, receipt)
    assert lab.unknown_target_clearance(h.campaign, binding)
    changed = copy.deepcopy(binding)
    changed["revision"] = "different"
    assert not lab.unknown_target_clearance(h.campaign, changed)


def test_manifest_replacement_fsyncs_parent_and_immutable_artifact(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    parents = []
    monkeypatch.setattr(lab, "fsync_directory", lambda path: parents.append(path))
    path = h.root / "durable.json"
    lab.durable_json(path, {"one": 1})
    lab.durable_json(path, {"two": 2}, replace=True)
    assert lab.read_json(path) == {"two": 2}
    assert parents == [h.root, h.root]


def test_campaign_freezes_exact_tools_and_rejects_changed_copy(h: Harness) -> None:
    config = lab.read_json(h.campaign / "config.json")
    assert Path(config["check_tools"]) == h.campaign / "check-tools"
    assert lab.check_tools_identity(Path(config["check_tools"])) == h.config["check_tools_sha256"]
    (h.root / "tools/bin/jq").write_bytes(b"external original changed")
    # Frozen campaign does not depend on mutable host originals.
    lab.validate_config(config, h.loader)
    (h.campaign / "check-tools/bin/jq").write_bytes(b"tampered frozen tool")
    with pytest.raises(ValueError, match="provenance mismatch"):
        h.run()
    assert not h.calls


@pytest.mark.parametrize("kind", ["extra", "symlink", "mode"])
def test_tool_bundle_is_narrow_and_identity_checked(h: Harness, kind: str) -> None:
    root = h.campaign / "check-tools"
    if kind == "extra":
        (root / "unapproved").write_bytes(b"not allowed")
    elif kind == "symlink":
        path = root / "lib/libjq.so.1"
        path.unlink()
        path.symlink_to(h.root / "frozen")
    else:
        (root / "bin/jq").chmod(0o644)
    with pytest.raises(ValueError):
        lab.check_tools_identity(root)


def test_full_checks_keep_exact_export_separate_from_trusted_overlay(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    def isolated(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(args)
        inputs, work = kwargs["inputs"], kwargs["work"]
        assert (inputs / "exact-candidate/scripts/controller.py").read_text() == "TRUSTED = True\n"
        assert not (inputs / "exact-candidate/scripts/lab_experiment.py").exists()
        assert (work / "scripts/lab_experiment.py").is_file()
        assert "--network" in args and args[args.index("--network") + 1] == "none"
        assert args.count("--mount") == 2
        assert "CI=1" in args
        receipt = lab.read_json(inputs / "inputs.json")
        assert set(receipt["candidate"]) == set(lab.tracked(h.repo, h.initial))
        return subprocess.CompletedProcess(args, 0, stdout=b"synthetic control only", stderr=b"")
    monkeypatch.setattr(lab, "isolated_check", isolated)
    report = lab.full_candidate_checks(attempt=h.root / "full-checks", repo=h.repo,
        revision=h.initial, image=h.config["image"], name="synthetic-check",
        check_tools=h.campaign / "check-tools", check_tools_sha256=h.config["check_tools_sha256"])
    assert len(calls) == 3 and report["passed"] is True
    assert report["exact_candidate_sha256"] != report["overlay_sha256"]
    assert report["cleanup_complete"] is True
    assert "scripts.lab_full_check_probe" in calls[0]


def test_probe_changes_only_install_fixture_root(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from scripts import lab_full_check_probe as probe
    original = object()
    module = SimpleNamespace(__file__="/candidate/tests/test_install_smoke.py", REPO_ROOT=tmp_path,
                             oracle=original)
    item = SimpleNamespace(nodeid="tests/test_install_smoke.py::test_hermes_plugin_install_register_and_search_smoke",
                           module=module)
    probe.pytest_collection_modifyitems([item])
    assert module.REPO_ROOT == Path("/check-inputs/exact-candidate")
    assert module.oracle is original
    with pytest.raises(ValueError, match="not collected"):
        probe.pytest_collection_modifyitems([])


@pytest.mark.parametrize("cleaned", [False, True])
def test_failed_session_cleanup_receipt_controls_pending_and_preserves_usage(h: Harness, cleaned: bool) -> None:
    def failed_session(**kwargs: Any) -> dict:
        private = kwargs["attempt"] / "private"
        private.mkdir(parents=True)
        lab.durable_json(private / "status.json", {
            "role": kwargs["role"], "session_id": "synthetic-paid-session", **lab.MODEL,
            "usage": [{"input_tokens": 123, "output_tokens": 12}], "cleanup_complete": cleaned})
        raise ValueError("Synthetic source-validation or cleanup failure")
    with pytest.raises(ValueError):
        lab.run_experiment(h.campaign, session=failed_session, loader=h.loader)
    manifest = h.manifest()
    report = lab.finalized_reports(h.campaign, manifest)[0]
    assert report["usage"][0]["usage"][0] == {"input_tokens": 123, "output_tokens": 12}
    assert report["reconciliation_required"] is not cleaned
    assert bool(manifest["pending"]) is not cleaned
    assert manifest["incumbent"] == h.initial


@pytest.mark.parametrize("infinite", [False, True])
def test_authoritative_checks_stream_candidate_output(h: Harness, monkeypatch: pytest.MonkeyPatch,
                                                     infinite: bool) -> None:
    """Real full-check caller and pipes, mocked daemon only; no candidate host execution."""
    import os
    import sys
    import tracemalloc
    from scripts import lab_process_output as output, lab_runtime
    calls, attached, children = [], [], []
    specs: dict[str, Any] = {}
    real_run = lab_runtime.run
    real_popen = subprocess.Popen
    ceiling = 1024 * 1024 + 5
    monkeypatch.setattr(output, "COMMAND_OUTPUT_BYTES", ceiling)
    code = ("import os\nwhile True: os.write(1,b'x'*8192); os.write(2,b'e'*8192)" if infinite else
            "import os; os.write(1,b'x'*1048576); os.write(2,b'error')")

    def daemon(args: list[str], **kwargs: Any) -> str:
        if args[0] != "docker":
            return real_run(args, **kwargs)
        calls.append(args)
        if args[:2] == ["docker", "create"]:
            cid = "synthetic-" + str(len(specs))
            name = args[args.index("--name") + 1]
            image = h.config["image"]
            specs[cid] = {
                "Id": cid, "Name": "/" + name, "Image": image,
                "Config": {"Labels": {"lk.lab": "ticket-69"}, "User": f"{os.getuid()}:{os.getgid()}",
                    "WorkingDir": "/candidate", "Entrypoint": ["/opt/lab-tools/bin/python"],
                    "Cmd": args[args.index(image) + 1:],
                    "Env": [args[i + 1] for i, a in enumerate(args) if a == "--env"]},
                "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                    "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "Memory": 2147483648,
                    "MemorySwap": 2147483648, "NanoCpus": 2000000000, "PidsLimit": 256,
                    "RestartPolicy": {"Name": "no"}, "Tmpfs": {
                        "/tmp": "rw,nosuid,nodev,size=536870912,mode=1777",
                        "/opt/data": "rw,nosuid,nodev,size=67108864,mode=1777"}},
                "Mounts": [{"Type": "bind", "Source": mount.split("src=", 1)[1].split(",")[0],
                            "Destination": mount.split("dst=", 1)[1].split(",")[0], "RW": False}
                           for i, a in enumerate(args) if a == "--mount" for mount in [args[i + 1]]],
                "State": {"Running": False, "ExitCode": 0}}
            return cid
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([specs[args[-1]]])
        if args[:2] == ["docker", "kill"]:
            specs[args[-1]]["State"]["Running"] = False
            return args[-1]
        if args[:2] in (["docker", "rm"], ["docker", "ps"]):
            return ""
        raise AssertionError(args)

    def attach(args: list[str], **kwargs: Any) -> Any:
        if args[0] != "docker":
            return real_popen(args, **kwargs)
        assert args[:3] == ["docker", "start", "--attach"] and args[-1] in specs
        assert "shell" not in kwargs and "timeout" not in kwargs
        assert set(kwargs["env"]) <= {"PATH", "HOME", "LANG"}
        attached.append(args)
        specs[args[-1]]["State"]["Running"] = infinite
        child = real_popen([sys.executable, "-c", code], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(lab_runtime, "run", daemon)
    monkeypatch.setattr(output.subprocess, "Popen", attach)
    attempt = h.root / "stream-checks"
    tracemalloc.start()
    try:
        report = lab.full_candidate_checks(attempt=attempt, repo=h.repo, revision=h.initial,
            image=h.config["image"], name="lk-lab-stream-check", check_tools=h.campaign / "check-tools",
            check_tools_sha256=h.config["check_tools_sha256"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
    assert len(attached) == 3 and all(child.poll() is not None for child in children)
    assert report["passed"] is not infinite and report["cleanup_complete"]
    assert lab.read_json(attempt / "checks.json") == report
    for check, args in zip(report["checks"], attached):
        receipt = lab.read_json(attempt / check["output_receipt"])
        assert receipt["output_limit_exceeded"] is infinite
        assert sum(receipt["stored_bytes"].values()) == ceiling
        assert all(len(receipt[k]) <= output.PREVIEW_BYTES for k in ("stdout", "stderr"))
        for key in ("stdout", "stderr"):
            assert (attempt / receipt["files"][key]).stat().st_size == receipt["stored_bytes"][key]
        if not infinite:
            assert (attempt / receipt["files"]["stdout"]).read_bytes() == b'x' * 1048576
            assert (attempt / receipt["files"]["stderr"]).read_bytes() == b'error'
        assert lab.read_json(attempt / check["cleanup_receipt"])["cleanup_complete"]
        assert ["docker", "rm", "--force", "--volumes", args[-1]] in calls
        assert (["docker", "kill", args[-1]] in calls) is infinite


def test_process_output_helper_is_controller_identity_input() -> None:
    assert "scripts/lab_process_output.py" in lab.controller_identity()
