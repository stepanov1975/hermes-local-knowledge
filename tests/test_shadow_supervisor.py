from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hermes_local_knowledge import cli, index, shadow, shadow_hooks, shadow_supervisor
from hermes_local_knowledge.config import Config, VerifiedRoutingSettings, resolve_config

# Only this disposable fake host changes test timing. Production has no hidden
# budget/lease environment overrides and never imports a test model.
FAKE_HOST = '''
import argparse, json, os, signal, time
from pathlib import Path
from types import SimpleNamespace
from hermes_local_knowledge import cli, shadow, shadow_hooks, shadow_supervisor
home = Path(os.environ['HERMES_HOME'])
scenario = json.loads((home / 'scenario.json').read_text())
shadow_supervisor.MAX_SECONDS = scenario.get('seconds', 8)
shadow_supervisor.MAX_LAUNCHES = scenario.get('launches', 16)
shadow_supervisor.POLL_SECONDS = .02
if scenario.get('idle_race'):
    original_delay = shadow_supervisor.work_delay
    def gated_idle(cfg):
        delay = original_delay(cfg)
        if delay is None and not (home / 'idle-snapshot').exists():
            (home / 'idle-snapshot').touch()
            end = time.monotonic() + 10
            while not (home / 'idle-release').exists() and time.monotonic() < end:
                time.sleep(.02)
        return delay
    shadow_supervisor.work_delay = gated_idle
original_claim = shadow._claim
if scenario.get('short_lease'):
    def claim(cfg, owner, lease_until):
        return original_claim(cfg, owner, time.time() + 1.2)
    shadow._claim = claim
class Model:
    def complete_structured(self, **kwargs):
        (home / 'model-started').write_text(str(os.getpid()))
        if scenario.get('kill_first') and not (home / 'killed').exists():
            (home / 'killed').touch()
            os.kill(os.getpid(), signal.SIGKILL)
        if scenario.get('descendant'):
            import subprocess, sys
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            (home / 'descendant').write_text(str(child.pid))
        if scenario.get('block'):
            deadline = time.monotonic() + 20
            while not (home / 'release').exists() and time.monotonic() < deadline:
                time.sleep(.01)
        return SimpleNamespace(parsed={'action': 'unresolved'}, usage={'total_tokens': 7})
parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest='command', required=True)
cli.setup_hermes_cli(sub.add_parser('local-knowledge'))
args = parser.parse_args()
# Exercise actual hook guards inside both process roles, with ready rows present.
assert shadow_hooks.on_session_finalize(session_id='test') is False
shadow_hooks.on_session_end(session_id='test')
if args.local_knowledge_command == 'routing-worker':
    with (home / 'launches').open('a') as out:
        out.write(str(os.getpid()) + '\\n')
# Linux regression only: adopt/reap the orphan in this disposable host, not in
# pytest or PID 1. A live descendant blocks waitpid and fails the bounded test.
reap_descendant = scenario.get('descendant') and args.local_knowledge_command == 'routing-supervisor'
if reap_descendant:
    import ctypes
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
try:
    result = cli.handle_hermes_cli(args, llm=Model())
except SystemExit as exc:
    result = exc.code
if reap_descendant:
    pid = int((home / 'descendant').read_text())
    _, status = os.waitpid(pid, 0)
    (home / 'descendant-exit').write_text(str(os.waitstatus_to_exitcode(status)))
raise SystemExit(result)
'''


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    for key in ("LOCAL_KNOWLEDGE_ROOT", "LOCAL_KNOWLEDGE_STATE_DIR", "HERMES_HOME",
                shadow_hooks.SHADOW_WORKER_ENV, shadow_hooks.OKF_WORKER_ENV):
        monkeypatch.delenv(key, raising=False)
    home, root = tmp_path / "profile", tmp_path / "source"
    home.mkdir()
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "restart.md").write_text("# Quartz restart runbook\nRestart Quartz safely.\n")
    (home / "config.yaml").write_text(
        f"local_knowledge:\n  source_root: {root}\n  okf:\n    enabled: false\n"
        "  verified_routing:\n    mode: shadow\n")
    config = resolve_config(home)
    index.build_index(root, config.state_dir, home, config.index_settings)
    fake = home / "hermes_cli"
    fake.mkdir()
    (fake / "__init__.py").touch()
    (fake / "main.py").write_text(FAKE_HOST)
    (home / "scenario.json").write_text("{}")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(home), str(Path(__file__).resolve().parents[1])]))
    monkeypatch.setattr(shadow_hooks, "resolve_config", lambda: config)
    return config


def capture(cfg: Config, count: int, *, start_number: int = 0) -> list[str]:
    ids = []
    for n in range(start_number, start_number + count):
        result = shadow.observe(
            cfg, user_request=f"Find Quartz restart procedure for service number {n}.",
            query=f"Quartz restart service {n}", artifact_type="", baseline_ids=[],
            session_id="test", task_id="task", turn_id=f"turn-{n}",
        )
        ids.append(result["case_id"])
    return ids


def scenario(cfg: Config, **settings: Any) -> None:
    (cfg.hermes_home / "scenario.json").write_text(json.dumps(settings))


def wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("subprocess condition not reached")


@pytest.fixture
def processes() -> Iterator[list[subprocess.Popen[Any]]]:
    children: list[subprocess.Popen[Any]] = []
    yield children
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def start(cfg: Config, processes: list[subprocess.Popen[Any]], *, wake_ns: int | None = None) -> subprocess.Popen[Any]:
    command = [sys.executable, "-m", "hermes_cli.main", "local-knowledge", "routing-supervisor",
               "--hermes-home", str(cfg.hermes_home), "--wake-ns", str(wake_ns or time.time_ns())]
    child = subprocess.Popen(command, env=shadow_supervisor.worker_env(cfg), cwd=cfg.hermes_home)
    processes.append(child)
    return child


def launches(cfg: Config) -> int:
    path = cfg.hermes_home / "launches"
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_turn_end_alone_drains_eight_cases_including_legacy_and_changed_baseline(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, processes: list[subprocess.Popen[Any]],
) -> None:
    ids = capture(cfg, 8)
    with shadow._connect(cfg, create=True) as conn:
        for key in ids[:2]:
            conn.execute("UPDATE cases SET lookup_context='{}' WHERE id=?", (key,))
        packet = json.loads(conn.execute("SELECT lookup_context FROM cases WHERE id=?", (ids[2],)).fetchone()[0])
        packet["baseline_fingerprint"] = "changed"
        conn.execute("UPDATE cases SET lookup_context=? WHERE id=?", (json.dumps(packet), ids[2]))
    monkeypatch.setattr(shadow_hooks, "_start_worker_reaper", processes.append)
    # Any in-process inference is a bug. The fake provider exists only in children.
    monkeypatch.setattr(shadow, "run_batch", lambda *a, **k: pytest.fail("inline model work"))
    assert shadow_supervisor.work_delay(cfg) is None
    shadow_hooks.on_session_end(session_id="test")
    assert len(processes) == 1
    assert processes[0].wait(timeout=15) == 0
    result = shadow.report(cfg)
    assert result["cases"] == {"unresolved": 8}
    assert result["ready"] == 0
    assert result["model_calls"] == 5
    assert result["usage"] == {"total_tokens": 35}
    assert launches(cfg) == 8  # More than both default and maximum batch sizes.
    assert shadow_supervisor.work_delay(cfg) is None


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL interruption proof is POSIX-specific")
@pytest.mark.parametrize("batch_size,expected_launches", [(1, 3), (2, 2)])
def test_killed_child_is_closed_after_lease_and_remaining_cases_progress_without_wake(
    cfg: Config, processes: list[subprocess.Popen[Any]], batch_size: int, expected_launches: int,
) -> None:
    config_file = cfg.hermes_home / "config.yaml"
    config_file.write_text(config_file.read_text() + f"    max_cases_per_worker: {batch_size}\n")
    capture(cfg, 3)
    shadow.finish_session(cfg, "test")
    scenario(cfg, kill_first=True, short_lease=True)
    process = start(cfg, processes)
    wait_for(lambda: (cfg.hermes_home / "killed").exists())
    with shadow._connect(cfg) as conn:
        running = dict(conn.execute("SELECT * FROM cases WHERE status='running' AND calls>0").fetchone())
    assert running["calls"] == 1 and running["stage"].endswith("_inflight")
    assert running["lease_until"] > time.time()
    assert shadow_supervisor.work_delay(cfg) is not None
    assert not shadow.has_work(cfg)  # Cannot reclaim a live lease.
    assert process.wait(timeout=15) == 0
    with shadow._connect(cfg) as conn:
        interrupted = dict(conn.execute("SELECT * FROM cases WHERE id=?", (running["id"],)).fetchone())
    assert interrupted["reason"] == "interrupted_ambiguous"
    assert interrupted["status"] == "unresolved"
    assert interrupted["calls"] == interrupted["total_calls"] == 1  # No replay.
    assert shadow.report(cfg)["cases"] == {"unresolved": 3}
    assert shadow.report(cfg)["model_calls"] == 3
    assert launches(cfg) == expected_launches


def test_duplicate_wakes_cannot_multiply_or_replenish_exhausted_allowance(
    cfg: Config, processes: list[subprocess.Popen[Any]],
) -> None:
    capture(cfg, 5)
    shadow.finish_session(cfg, "test")
    scenario(cfg, launches=2, block=True)
    wake = time.time_ns()
    owner = start(cfg, processes, wake_ns=wake)
    wait_for(lambda: (cfg.hermes_home / "model-started").exists())
    duplicate = start(cfg, processes, wake_ns=wake)
    assert duplicate.wait(timeout=5) == 0
    assert owner.poll() is None
    assert launches(cfg) == 1
    (cfg.hermes_home / "release").touch()
    assert owner.wait(timeout=15) == 1
    # The same wake delayed until AFTER exhaustion must not get a new budget.
    delayed = start(cfg, processes, wake_ns=wake)
    assert delayed.wait(timeout=5) == 0
    assert launches(cfg) == 2
    result = shadow.report(cfg)
    assert result["ready"] == 3 and result["model_calls"] == 2
    assert shadow._path(cfg).with_name("supervisor.log").read_text() == "launch_budget\n"
    # A genuinely later external wake may spend one NEW finite allowance.
    assert start(cfg, processes).wait(timeout=15) == 1
    assert launches(cfg) == 4


def test_elapsed_exhaustion_kills_blocked_child_without_recursive_recovery(
    cfg: Config, processes: list[subprocess.Popen[Any]],
) -> None:
    capture(cfg, 3)
    shadow.finish_session(cfg, "test")
    scenario(cfg, seconds=1.5, block=True)
    before = time.monotonic()
    assert start(cfg, processes).wait(timeout=10) == 1
    assert time.monotonic() - before < 8
    assert launches(cfg) == 1
    assert shadow.report(cfg)["cases"] == {"pending": 2, "running": 1}
    assert shadow._path(cfg).with_name("supervisor.log").read_text() == "time_budget\n"
    # Reaped direct child: no surviving process to charge or respawn a supervisor.
    pid = int((cfg.hermes_home / "model-started").read_text())
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.skipif(sys.platform != "linux", reason="uses a real Linux child subreaper for zombie-free proof")
def test_elapsed_exhaustion_kills_and_reaps_worker_descendant(
    cfg: Config, processes: list[subprocess.Popen[Any]],
) -> None:
    capture(cfg, 3)
    shadow.finish_session(cfg, "test")
    scenario(cfg, seconds=3, block=True, descendant=True)
    before = time.monotonic()
    owner = start(cfg, processes)
    try:
        wait_for(lambda: (cfg.hermes_home / "descendant").exists())
        descendant = int((cfg.hermes_home / "descendant").read_text())
        worker = int((cfg.hermes_home / "model-started").read_text())
        os.kill(descendant, 0)  # An actual live grandchild, not a mocked Popen.
        assert owner.wait(timeout=7) == 1
        assert time.monotonic() - before < 9
        assert (cfg.hermes_home / "descendant-exit").read_text() == str(-signal.SIGKILL)
        for pid in (worker, descendant):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)  # Reaped too: do not mistake a zombie for cleanup.
        assert launches(cfg) == 1
        assert shadow.report(cfg)["cases"] == {"pending": 2, "running": 1}
        assert shadow._path(cfg).with_name("supervisor.log").read_text() == "time_budget\n"
    finally:
        # Also clean up under the old direct-child-only implementation: the
        # subreaper is still alive waiting for its adopted descendant to exit.
        for name in ("descendant", "model-started"):
            path = cfg.hermes_home / name
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        owner.wait(timeout=10)


def test_waiting_on_live_lease_consumes_elapsed_not_launch_budget(
    cfg: Config, processes: list[subprocess.Popen[Any]],
) -> None:
    capture(cfg, 2)
    shadow.finish_session(cfg, "test")
    with shadow._connect(cfg, create=True) as conn:
        conn.execute("INSERT INTO lease VALUES (1,'other',?)", (time.time() + 60,))
    scenario(cfg, seconds=.3)
    assert start(cfg, processes).wait(timeout=5) == 1
    assert launches(cfg) == 0
    assert shadow.report(cfg)["ready"] == 2


def test_off_recursion_and_finalize_fallback_do_not_infer_inline(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture(cfg, 1)
    shadow.finish_session(cfg, "test")
    spawned: list[Config] = []

    def spawn(config: Config) -> bool:
        spawned.append(config)
        return True

    monkeypatch.setattr(shadow_hooks, "_spawn_worker", spawn)
    assert shadow_hooks.on_session_finalize(session_id="test")
    assert spawned == [cfg]
    for key in (shadow_hooks.SHADOW_WORKER_ENV, shadow_hooks.OKF_WORKER_ENV):
        monkeypatch.setenv(key, "1")
        shadow_hooks.on_session_end(session_id="test")
        assert not shadow_hooks.on_session_finalize(session_id="test")
        monkeypatch.delenv(key)
    assert spawned == [cfg]
    off = replace(cfg, verified_routing=VerifiedRoutingSettings())
    monkeypatch.setattr(shadow_hooks, "resolve_config", lambda: off)
    monkeypatch.setattr(shadow_supervisor, "resolve_config", lambda *a: off)
    assert shadow_supervisor.work_delay(off) is None
    shadow_hooks.on_session_end(session_id="test")
    assert not shadow_hooks.on_session_finalize(session_id="test")
    assert shadow_supervisor.run_supervisor(hermes_home=cfg.hermes_home) == 0
    assert not shadow._path(cfg).with_name("supervisor.sqlite").exists()
    assert spawned == [cfg]
    with pytest.raises(SystemExit):
        cli.main(["routing-supervisor"])  # Host-only, no standalone inference API.


def test_disable_during_child_stops_subsequent_launches(
    cfg: Config, processes: list[subprocess.Popen[Any]],
) -> None:
    capture(cfg, 3)
    shadow.finish_session(cfg, "test")
    scenario(cfg, block=True)
    process = start(cfg, processes)
    wait_for(lambda: (cfg.hermes_home / "model-started").exists())
    config_file = cfg.hermes_home / "config.yaml"
    config_file.write_text(config_file.read_text().replace("mode: shadow", "mode: off"))
    (cfg.hermes_home / "release").touch()
    assert process.wait(timeout=15) == 0
    assert launches(cfg) == 1
    assert shadow.report(cfg)["ready"] == 2
    assert shadow._path(cfg).with_name("supervisor.log").read_text() == "disabled_or_changed\n"


@pytest.mark.parametrize("started", [False, True])
def test_expired_running_retries_only_unstarted_cases(
    cfg: Config, processes: list[subprocess.Popen[Any]], started: bool,
) -> None:
    capture(cfg, 1)
    shadow.finish_session(cfg, "test")
    claimed = shadow._claim(cfg, "interrupted", time.time() - 1)
    assert len(claimed) == 1
    if started:
        with shadow._connect(cfg, create=True) as conn:
            conn.execute("UPDATE cases SET calls=1,total_calls=1,stage='investigator_inflight'")
    assert start(cfg, processes).wait(timeout=10) == 0
    assert shadow.report(cfg)["cases"] == {"unresolved": 1}
    assert shadow.report(cfg)["model_calls"] == 1
    assert (cfg.hermes_home / "model-started").exists() is not started
    with shadow._connect(cfg) as conn:
        reason = conn.execute("SELECT reason FROM cases").fetchone()[0]
    assert (reason == "interrupted_ambiguous") is started
    assert launches(cfg) == 1


@pytest.mark.parametrize("limit,remaining", [(2, 0), (1, 1)])
def test_wake_racing_idle_exit_is_not_lost_or_given_a_fresh_budget(
    cfg: Config, processes: list[subprocess.Popen[Any]], limit: int, remaining: int,
) -> None:
    capture(cfg, 1)
    shadow.finish_session(cfg, "test")
    scenario(cfg, idle_race=True, launches=limit)
    owner = start(cfg, processes)
    wait_for(lambda: (cfg.hermes_home / "idle-snapshot").exists())
    # The owner has read an empty queue but still holds its lock. This sole new
    # wake exits as a duplicate; the owner must recheck after releasing the lock.
    capture(cfg, 1, start_number=1)
    shadow.finish_session(cfg, "test")
    assert start(cfg, processes).wait(timeout=3) == 0
    (cfg.hermes_home / "idle-release").touch()
    assert owner.wait(timeout=15) == (1 if remaining else 0)
    assert shadow.report(cfg)["ready"] == remaining
    assert launches(cfg) == limit
    assert shadow.report(cfg)["model_calls"] == limit
