"""Experiment-local lab filesystem guard; no scheduler or production operations."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import threading
from typing import Any, Callable, Iterator

INTERVAL_SECONDS = 60
_CURRENT: ContextVar[DiskGuard | None] = ContextVar("lab_disk_guard", default=None)


class LabDiskPaused(RuntimeError):
    """Lab filesystem safety could not be established; operator action required."""


def available_space(path: Path) -> dict[str, int]:
    fs = os.statvfs(path)
    # df basis: used = blocks - bfree; usable total = used + bavail.
    # Reserved blocks are neither unprivileged available nor usable total.
    used = fs.f_blocks - fs.f_bfree
    total = used + fs.f_bavail
    if used < 0 or fs.f_bavail < 0 or total <= 0:
        raise OSError("Invalid filesystem accounting")
    return {"available_blocks": fs.f_bavail, "usable_blocks": total}


class DiskGuard:
    def __init__(self, path: Path, pause: Callable[[dict[str, Any]], None]) -> None:
        self.path = path
        self.pause = pause
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.failed = False
        self.workers: list[Callable[[], None]] = []
        self.thread = threading.Thread(target=self._monitor, name="lab-disk-guard")

    def sample(self) -> None:
        with self.lock:
            if self.failed:
                return
            try:
                space = available_space(self.path)
                if space["available_blocks"] * 10 >= space["usable_blocks"]:
                    return
                reason = "available_space_below_10_percent"
            except OSError:
                space = {}
                reason = "available_space_unknown"
            self.failed = True
            alert = {"kind": "lab_disk_paused", "reason": reason, **space,
                     "percentage_basis": "bavail / (blocks - bfree + bavail)",
                     "action": "Free space on the lab filesystem; inspect owned-worker cleanup and reconcile the paused campaign before manually resuming."}
            # Persist the pause/alert BEFORE potentially blocked Docker stop calls.
            try:
                self.pause(alert)
            finally:
                for stop in list(self.workers):
                    try:
                        stop()
                    except Exception:
                        # The worker owner's normal finally must retry cleanup.
                        pass

    def _monitor(self) -> None:
        while not self.stopping.wait(INTERVAL_SECONDS):
            self.sample()
            if self.failed:
                return

    def check(self) -> None:
        with self.lock:
            if self.failed:
                raise LabDiskPaused("Lab filesystem unsafe; campaign paused; inspect disk-alert.json")

    @contextmanager
    def action(self) -> Iterator[None]:
        """Fence a short dispatch/publication against detection; never hold over waits."""
        with self.lock:
            self.check()
            yield

    def __enter__(self) -> DiskGuard:
        self.sample()
        self.check()
        self.token = _CURRENT.set(self)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stopping.set()
        self.thread.join()
        _CURRENT.reset(self.token)


@contextmanager
def action() -> Iterator[None]:
    guard = _CURRENT.get()
    if guard is None:
        yield
    else:
        with guard.action():
            yield


def checkpoint() -> None:
    guard = _CURRENT.get()
    if guard is not None:
        guard.check()


@contextmanager
def owned_worker(stop: Callable[[], None]) -> Iterator[None]:
    guard = _CURRENT.get()
    if guard is None:
        yield
        return
    with guard.action():
        guard.workers.append(stop)
    try:
        yield
    finally:
        with guard.lock:
            guard.workers.remove(stop)
