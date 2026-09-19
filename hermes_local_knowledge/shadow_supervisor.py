"""Finite host-internal owner for shadow batches, not a durable scheduler.

A separate SQLite write lock works on every platform supported by the index
locks, without holding a queue transaction or inheriting a lock into children.
Only a surviving supervisor can recover an interrupted child. Nothing respawns
this process, and duplicate wakes never extend its launch or elapsed allowance.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

from . import shadow
from .config import Config, resolve_config
from .okf import OKF_WORKER_ENV, _detached_process_kwargs

MAX_SECONDS = 900
MAX_LAUNCHES = 16
POLL_SECONDS = 1.0
SHADOW_WORKER_ENV = "LOCAL_KNOWLEDGE_SHADOW_WORKER"


def worker_env(cfg: Config) -> dict[str, str]:
    env = os.environ.copy()
    env[SHADOW_WORKER_ENV] = "1"
    env[OKF_WORKER_ENV] = "1"
    env["HERMES_HOME"] = str(cfg.hermes_home)
    return env


def work_delay(cfg: Config) -> float | None:
    """Read-only wake decision, including live leases that need a future check."""
    if not shadow._enabled(cfg) or not shadow._path(cfg).is_file():
        return None
    with closing(shadow._connect(cfg)) as conn:
        pending = conn.execute("SELECT 1 FROM cases WHERE status='pending' AND ready=1 LIMIT 1").fetchone()
        running = conn.execute("SELECT MIN(lease_until) FROM cases WHERE status='running'").fetchone()[0]
        if not pending and running is None:
            return None
        lease = conn.execute("SELECT MAX(until) FROM lease").fetchone()[0] or 0
        return max(0.0, max(lease, running if not pending else 0) - time.time())


def _finish(gate: sqlite3.Connection, cfg: Config, outcome: str) -> None:
    gate.execute("INSERT INTO wake VALUES (1,?) ON CONFLICT(id) DO UPDATE SET finished_ns=excluded.finished_ns",
                 (0 if outcome in {"idle", "disabled_or_changed"} else time.time_ns(),))
    # Publish structural status before releasing ownership; never task/provider text.
    try:
        shadow._path(cfg).with_name("supervisor.log").write_text(outcome + "\n", encoding="utf-8")
    except OSError:
        pass
    gate.commit()


def _run(cfg: Config, deadline: float, gate: sqlite3.Connection) -> str:
    launches = 0
    while time.monotonic() < deadline:
        current = resolve_config(cfg.hermes_home)
        # Config edits must not send later children into a different queue.
        if not shadow._enabled(current) or shadow._path(current) != shadow._path(cfg):
            return "disabled_or_changed"
        delay = work_delay(current)
        if delay is None:
            # Release before the final read: a wake racing the idle check either
            # acquires ownership itself, or its ready rows are seen here. Keep
            # this same launch/time allowance if we reacquire, never recurse.
            _finish(gate, cfg, "idle")
            if work_delay(current) is None:
                return "idle"
            try:
                gate.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                return "idle"  # A successor owns the work now.
            if gate.execute("SELECT finished_ns FROM wake WHERE id=1").fetchone()[0]:
                gate.rollback()  # A successor exhausted its allowance in the gap.
                return "idle"
            continue
        if launches >= MAX_LAUNCHES:
            return "launch_budget"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if delay > 0:
            time.sleep(min(delay, POLL_SECONDS, remaining))
            continue
        launches += 1  # Never replenish, including failed/cheap/empty batches.
        with subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.main", "local-knowledge", "routing-worker",
             "--hermes-home", str(cfg.hermes_home)],
            cwd=str(cfg.hermes_home), env=worker_env(cfg), close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **_detached_process_kwargs(),
        ) as child:
            try:
                child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                outcome = "time_budget"
                try:
                    if os.name == "nt":
                        # Terminate descendants before their parent disappears.
                        # Bound taskkill itself; failure must not look like success.
                        subprocess.run(
                            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=True, timeout=5,
                        )
                    else:
                        # The worker owns a new session/group, not ours.
                        os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The complete group already exited.
                except (OSError, subprocess.SubprocessError):
                    outcome = "supervisor_error"
                finally:
                    # Reap the direct child even when tree termination failed.
                    child.kill()
                    child.wait()
                return outcome
        # A failed child may have spent tokens: only the queue's existing expiry
        # and fencing logic can close its ambiguous claims. Never replay them.
    return "time_budget"


def run_supervisor(*, hermes_home: Path | str | None = None, wake_ns: int | None = None) -> int:
    """Coalesce overlapping wakes, own one fixed allowance, and then exit."""
    deadline = time.monotonic() + MAX_SECONDS
    wake_ns = time.time_ns() if wake_ns is None else wake_ns
    cfg = resolve_config(hermes_home)
    if not shadow._enabled(cfg) or not shadow._path(cfg).is_file():
        return 0
    lock_path = shadow._path(cfg).with_name("supervisor.sqlite")
    # State lives in the queue's private profile/root namespace. Never unlink the
    # lock file: replacing its inode could allow concurrent owners.
    lock_path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(lock_path, timeout=0)) as gate:
        try:
            gate.execute("CREATE TABLE IF NOT EXISTS wake (id INTEGER PRIMARY KEY, finished_ns INTEGER NOT NULL)")
            gate.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return 0  # Another supervisor owns this namespace; no waiting chain.
        previous = gate.execute("SELECT finished_ns FROM wake WHERE id=1").fetchone()
        if previous and wake_ns <= previous[0]:
            return 0  # A delayed duplicate must not open a second spend allowance.
        outcome = "supervisor_error"
        try:
            outcome = _run(cfg, deadline, gate)
        except (OSError, sqlite3.Error):
            outcome = "supervisor_error"
        finally:
            if gate.in_transaction:
                _finish(gate, cfg, outcome)
        return 1 if outcome in {"launch_budget", "time_budget", "supervisor_error"} else 0
