"""Review regressions: authoritative usage paths and independent pause ordering."""
import json
import threading

import pytest

from scripts import lab_disk_guard as disk
from scripts import lab_experiment as lab
from scripts import lab_recovery as recovery
from tests.test_lab_experiment import Harness
from tests.test_lab_recovery import pending_candidate


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


@pytest.mark.parametrize("directory", ["scratch", "output"])
def test_recovery_usage_ignores_worker_owned_result_files(h, directory):
    attempt = pending_candidate(h)
    root = attempt / "implementation" / directory
    root.mkdir(parents=True, exist_ok=True)
    (root / "benchmark-result.json").write_text('[{"passed": true}]')
    (root / "forged-result.json").write_text(json.dumps({
        "role": "review", "session_id": "worker-output-not-authority",
        "usage": [{"input_tokens": 999999}]}))
    report = h.run()
    assert report["outcome"] == "accepted" and report["attempt_id"] == attempt.name
    assert len(report["usage"]) == 2
    assert all(s["session_id"] != "worker-output-not-authority" for s in report["usage"])
    assert all(s["usage"] == [{"input_tokens": 10}] for s in report["usage"])


@pytest.mark.parametrize("directory", ["scratch", "output"])
def test_completion_scan_ignores_worker_private_directories(tmp_path, directory):
    private = tmp_path / "implementation" / directory / "private"
    private.mkdir(parents=True)
    for name in ("request-1.json", "wire-1.json", "tool-intent-1-0.json"):
        (private / name).write_text("{}")
    assert recovery.session_completion(tmp_path) == "settled"


def test_recovery_pause_publication_serializes_concurrent_disk_pause(h, monkeypatch):
    attempt = pending_candidate(h)
    calls = len(h.calls)
    low = [False]
    monkeypatch.setattr(disk, "available_space", lambda path: {
        "available_blocks": 9 if low[0] else 20, "usable_blocks": 100})
    def uncertain(path):
        assert path == attempt
        raise recovery.RecoveryPaused("resource_inspection_unavailable")
    monkeypatch.setattr(recovery, "settle_resources", uncertain)
    durable = lab.durable_json
    samplers = []
    entered, completed = threading.Event(), threading.Event()
    sampled_during_write = []
    def write(path, value, *, replace=False):
        if path.name == "recovery-alert.json":
            guard = disk._CURRENT.get()
            assert guard is not None
            low[0] = True
            def sample():
                entered.set()
                guard.sample()
                completed.set()
            sampler = threading.Thread(target=sample)
            samplers.append(sampler)
            sampler.start()
            assert entered.wait(5)
            sampled_during_write.append(completed.wait(0.1))
        durable(path, value, replace=replace)
    monkeypatch.setattr(lab, "durable_json", write)
    with pytest.raises(ValueError, match="resource_inspection_unavailable"):
        h.run()
    for sampler in samplers:
        sampler.join(timeout=5)
        assert not sampler.is_alive()
    assert sampled_during_write == [False] and completed.is_set()
    assert h.manifest()["paused"] and h.manifest().get("pause_source") != "recovery"
    low[0] = False
    with pytest.raises(ValueError, match="paused or pending"):
        h.run(recover_only=True)
    assert h.manifest()["pending"] == attempt.name and len(h.calls) == calls
    assert h.manifest()["reports"] == []
