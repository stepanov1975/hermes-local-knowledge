"""PR50 regressions: private state and bounded applicability admission."""
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hermes_local_knowledge import index, shadow, shadow_sources
from hermes_local_knowledge.config import Config, IndexSettings, VerifiedRoutingSettings


@pytest.fixture
def cfg(tmp_path):
    root, home, state = (tmp_path / name for name in ("corpus", "profile", "state"))
    root.mkdir()
    home.mkdir()
    (root / "docs").mkdir()
    for i in range(40):
        (root / "docs" / f"item-{i}.md").write_text(f"# Quartz reference {i}\nScope {i}.\n")
    config = Config(source_root=root, hermes_home=home, state_dir=state,
                    index_settings=IndexSettings(), verified_routing=VerifiedRoutingSettings(mode="shadow"))
    index.build_index(root, state, home, config.index_settings)
    return config


def capture(cfg, query="Quartz restart", baseline=None):
    result = shadow.observe(cfg, user_request="Find the Quartz restart reference", query=query,
                            artifact_type="", session_id="session", task_id="task", turn_id="turn",
                            baseline_ids=baseline or [])
    assert result["status"] == "observed"
    with shadow._connect(cfg) as conn:
        return dict(conn.execute("SELECT * FROM cases WHERE id=?", (result["case_id"],)).fetchone())


def item(i):
    return f"runbook:docs-item-{i}"


def prior(cfg, ids, name="prior"):
    return {"id": name, "query": "Quartz restart", "user_request": "Find Quartz reference",
            "artifact_type": "", "baseline_ids": "[]", "lookup_context": json.dumps(shadow._lookup_context(None)),
            "verified_at": time.time(), "result": json.dumps({"contract_version": shadow.VERIFICATION_CONTRACT,
            "route_ids": ids[:1], "sources": [shadow_sources.identity(shadow_sources.read_source(cfg, i)) for i in ids]})}


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_existing_private_modes_repaired_without_chmod_shared_state(cfg):
    path = shadow._path(cfg)
    path.parent.mkdir(parents=True)
    path.touch()
    cfg.state_dir.chmod(0o755)
    path.parent.parent.chmod(0o755)
    path.parent.chmod(0o755)
    path.chmod(0o644)
    capture(cfg)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.parent.parent.stat().st_mode & 0o777 == 0o700
    assert cfg.state_dir.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks and mode bits")
@pytest.mark.parametrize("component", ["shadow", "namespace", "database"])
@pytest.mark.parametrize("dangling", [False, True])
def test_private_state_symlinks_fail_closed_without_touching_targets(cfg, tmp_path, component, dangling):
    path = shadow._path(cfg)
    link = {"shadow": path.parent.parent, "namespace": path.parent, "database": path}[component]
    target = tmp_path / "unrelated"
    if not dangling:
        if component == "database":
            target.write_bytes(b"unrelated database sentinel")
            target.chmod(0o644)
        else:
            target.mkdir(mode=0o755)
            (target / "sentinel").write_bytes(b"unrelated directory sentinel")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=component != "database")
    before = target.stat() if target.exists() else None
    with pytest.raises(OSError, match="symlink_private_state"):
        shadow._connect(cfg)
    result = shadow.observe(cfg, user_request="Find the Quartz restart reference", query="Quartz restart",
                            artifact_type="", session_id="session", task_id="task", turn_id="turn",
                            baseline_ids=[])
    assert result == {"status": "skipped", "reason": "capture_error"}
    assert link.is_symlink()
    if before is None:
        assert not target.exists()
    else:
        after = target.stat()
        assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)
        if component == "database":
            assert target.read_bytes() == b"unrelated database sentinel"
        else:
            assert list(target.iterdir()) == [target / "sentinel"]
            assert (target / "sentinel").read_bytes() == b"unrelated directory sentinel"


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor permissions")
@pytest.mark.parametrize("component", ["shadow", "namespace", "database"])
def test_private_permission_repair_pins_inodes_against_symlink_swap(cfg, tmp_path, monkeypatch, component):
    path = shadow._path(cfg)
    path.parent.mkdir(parents=True)
    path.touch()
    entry = {"shadow": path.parent.parent, "namespace": path.parent, "database": path}[component]
    target = tmp_path / "unrelated"
    if component == "database":
        target.write_bytes(b"sentinel")
        target.chmod(0o644)
    else:
        target.mkdir(mode=0o755)
    original_mode = target.stat().st_mode
    real_fchmod = os.fchmod
    swapped = False

    def swap_then_chmod(fd, mode):
        nonlocal swapped
        if not swapped:
            swapped = True
            entry.rename(entry.with_name(entry.name + "-original"))
            entry.symlink_to(target, target_is_directory=component != "database")
        real_fchmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", swap_then_chmod)
    with pytest.raises(OSError):
        shadow._connect(cfg, create=True)
    assert swapped
    assert target.stat().st_mode == original_mode
    if component == "database":
        assert target.read_bytes() == b"sentinel"
    else:
        assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks and mode bits")
def test_configured_shared_state_symlink_remains_supported_and_unmodified(cfg):
    state = cfg.state_dir
    actual = state.with_name("actual-state")
    state.rename(actual)
    actual.chmod(0o755)
    state.symlink_to(actual, target_is_directory=True)
    capture(cfg)
    assert actual.stat().st_mode & 0o777 == 0o755
    assert shadow._path(cfg).stat().st_mode & 0o777 == 0o600


def test_report_off_reads_retained_evidence_without_mutation(cfg):
    capture(cfg)
    with shadow._connect(cfg, create=True) as conn:
        conn.execute("UPDATE cases SET status='running',total_calls=2,elapsed_seconds=7,lease_until=0")
    before = shadow._path(cfg).read_bytes()
    off = replace(cfg, verified_routing=VerifiedRoutingSettings())
    report = shadow.report(off)
    assert report["mode"] == "off"
    assert report["cases"] == {"running": 1}
    assert report["model_calls"] == 2
    assert report["elapsed_seconds"] == 7
    assert report["counters"]["captures"] == 1
    assert shadow._path(cfg).read_bytes() == before


def test_report_off_missing_does_not_create(cfg):
    assert shadow.report(replace(cfg, verified_routing=VerifiedRoutingSettings()))["cases"] == {}
    assert not shadow._path(cfg).exists()


def test_stopword_overlap_does_not_shortlist(cfg):
    old = capture(cfg, "the for Quartz")
    result = {"contract_version": shadow.VERIFICATION_CONTRACT, "route_ids": [item(31)]}
    with shadow._connect(cfg, create=True) as conn:
        conn.execute("UPDATE cases SET status='ai_verified',verified_at=?,result=? WHERE id=?",
                     (time.time(), json.dumps(result), old["id"]))
    row = capture(cfg, "the for Nebula")
    assert shadow._reuse_candidates(cfg, row, shadow._task_packet(row)) == []
    row = capture(cfg, "Quartz status")
    assert len(shadow._reuse_candidates(cfg, row, shadow._task_packet(row))) == 1


@pytest.mark.parametrize("steal", [False, True])
def test_failed_provider_elapsed_is_fenced_and_never_retried(cfg, monkeypatch, steal):
    row = capture(cfg)
    shadow.finish_session(cfg, "session")
    clock = [100.0]
    monkeypatch.setattr(shadow.time, "monotonic", lambda: clock[0])
    calls = []
    class Failing:
        def complete_structured(self, **kwargs):
            calls.append(kwargs)
            clock[0] += 9
            if steal:
                with shadow._connect(cfg, create=True) as conn:
                    conn.execute("UPDATE lease SET owner='other'")
            raise RuntimeError("SECRET provider details")
    shadow.run_batch(cfg, llm=Failing())
    with shadow._connect(cfg) as conn:
        saved = dict(conn.execute("SELECT * FROM cases WHERE id=?", (row["id"],)).fetchone())
    assert saved["elapsed_seconds"] == (0 if steal else 9)
    assert saved["total_calls"] == 1
    assert "SECRET" not in repr(saved)
    if not steal:
        assert saved["reason"] == "interrupted_ambiguous"
        assert shadow.run_batch(cfg, llm=Failing())["claimed"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("reject_first", [False, True])
def test_full_baseline_can_reuse_and_rejected_candidate_cannot_poison(cfg, monkeypatch, reject_first):
    baseline = [item(i) for i in range(30)]
    row = capture(cfg, baseline=baseline)
    good = prior(cfg, [item(35), item(36), item(37)], "good")
    shortlisted = [good]
    if reject_first:
        bad = prior(cfg, [item(30), item(31), item(32)], "bad")
        shortlisted.insert(0, bad)
        real_read = shadow_sources.Evidence.read
        def read(self, artifact_id, **kwargs):
            if artifact_id == item(32):
                raise ValueError("source_changed_during_verification")
            return real_read(self, artifact_id, **kwargs)
        monkeypatch.setattr(shadow_sources.Evidence, "read", read)
    monkeypatch.setattr(shadow, "_reuse_candidates", lambda *args: shortlisted)
    packets = []
    def call(*args, **kwargs):
        packet = kwargs["packet"]
        packets.append(packet)
        assert [r["case_id"] for r in packet["stored_routes"]] == ["good"]
        assert len(packet["candidates"]) <= 30 + shadow_sources.MAX_CANDIDATES
        assert len(packet["sources"]) <= shadow_sources.MAX_SOURCES
        assert not any(s["id"] in {item(30), item(31), item(32)} for s in packet["sources"])
        source = next(s for s in packet["sources"] if s["id"] == item(35))
        return {"verdict": "applicable", "case_id": "good", "route_ids": [item(35)], "lookup_supported": True,
                "citations": [{k: source[k] for k in ("id", "locator", "sha256")} | {"start_line": 1, "end_line": 1}],
                "baseline_review": [{"id": i, "disposition": "not_useful", "covered_by": [],
                                     "reason": "Unrelated scope"} for i in baseline]}
    monkeypatch.setattr(shadow, "_call", call)
    result = shadow._applicable(cfg, llm=SimpleNamespace(), row=row, owner="test", deadline=999,
                                task=shadow._task_packet(row))
    assert result is not None and result["matched_case_id"] == "good"
    assert len(packets) == 1


def test_source_budget_rejection_preserves_prior_and_later_candidates(cfg, monkeypatch):
    row = capture(cfg, baseline=[item(i) for i in range(30)])
    shortlisted = [prior(cfg, [item(30)], "first"),
                   prior(cfg, [item(i) for i in range(31, 39)], "too_many"),
                   prior(cfg, [item(39)], "last")]
    monkeypatch.setattr(shadow, "_reuse_candidates", lambda *args: shortlisted)
    packets = []
    def call(*args, **kwargs):
        packets.append(kwargs["packet"])
        return {"verdict": "unresolved"}
    monkeypatch.setattr(shadow, "_call", call)
    assert shadow._applicable(cfg, llm=None, row=row, owner="test", deadline=999,
                              task=shadow._task_packet(row)) is None
    assert len(packets) == 1
    packet = packets[0]
    assert [r["case_id"] for r in packet["stored_routes"]] == ["first", "last"]
    assert len(packet["sources"]) <= shadow_sources.MAX_SOURCES
    assert len(packet["candidates"]) == 32
    assert not set(item(i) for i in range(31, 39)) & {s["id"] for s in packet["sources"]}
