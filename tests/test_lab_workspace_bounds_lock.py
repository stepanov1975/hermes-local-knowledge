"""Offline entry-budget and real Linux inherited-flock regression proofs."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest

from scripts import lab_workspace as source


@pytest.mark.parametrize("shape", ["shallow", "deep"])
def test_empty_directories_consume_budget_before_descent_or_read(tmp_path, monkeypatch, shape):
    root = tmp_path / "work"
    root.mkdir()
    (root / "source.py").write_text("must not be read")
    parent = root
    for i in range(80):
        child = parent / str(i)
        child.mkdir()
        if shape == "deep":
            parent = child
    monkeypatch.setattr(source, "MAX_ENTRIES", 32, raising=False)
    scanned = []
    yielded = []
    scandir = os.scandir

    class BoundedScan:
        def __init__(self, path):
            scanned.append(Path(path))
            self.scan = scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.scan.close()

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.scan)
            yielded.append(entry.path)
            return entry

    monkeypatch.setattr(source.os, "scandir", BoundedScan)
    monkeypatch.setattr(source, "chunks", lambda *a: pytest.fail("content read before inventory"))
    with pytest.raises(ValueError, match="allowance"):
        source.read_files(root)
    assert len(yielded) == 33
    assert len(scanned) <= 32
    if shape == "shallow":
        assert scanned == [root]  # Not even the first empty child is descended into.


def test_deep_empty_tree_has_realistic_depth_bound(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    parent = root
    for _ in range(66):
        parent = parent / "d"
        parent.mkdir()
    monkeypatch.setattr(source, "chunks", lambda *a: pytest.fail("content read before inventory"))
    with pytest.raises(ValueError, match="depth allowance"):
        source.read_files(root)


def test_exact_entry_budget_includes_ignored_cache_directories(tmp_path, monkeypatch):
    (tmp_path / "package").mkdir()
    (tmp_path / "package/a.py").write_text("ok")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "ignored.pyc").write_bytes(b"ignored")
    monkeypatch.setattr(source, "MAX_ENTRIES", 3, raising=False)
    assert set(source.inventory(tmp_path, ignore_caches=True)) == {"package/a.py"}
    monkeypatch.setattr(source, "MAX_ENTRIES", 2)
    with pytest.raises(ValueError, match="allowance"):
        source.inventory(tmp_path, ignore_caches=True)


@pytest.mark.parametrize("kind", ["directory", "cache", "file", "hardlink", "fifo", "root"])
def test_workspace_safety_unchanged(tmp_path, kind):
    root = tmp_path / "work"
    root.mkdir()
    target = tmp_path / "target"
    if kind in {"directory", "cache", "root"}:
        target.mkdir()
        if kind == "root":
            root.rmdir()
            root.symlink_to(target, target_is_directory=True)
        else:
            (root / ("__pycache__" if kind == "cache" else "link")).symlink_to(target, target_is_directory=True)
    elif kind == "file":
        target.write_text("outside")
        (root / "link").symlink_to(target)
    elif kind == "hardlink":
        target.write_text("outside")
        os.link(target, root / "link")
    else:
        os.mkfifo(root / "fifo")
    with pytest.raises(ValueError):
        source.inventory(root, ignore_caches=True)


def test_recovery_rm_child_keeps_real_flock_after_controller_death(tmp_path):
    """Run actual settle_resources; only the external Docker executable is synthetic."""
    ready, release = tmp_path / "ready", tmp_path / "release"
    os.mkfifo(ready)
    os.mkfifo(release)
    ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    attempt = tmp_path / "attempt"
    receipts = attempt / "resources"
    receipts.mkdir(parents=True)
    args = ["docker", "create", "--name", "owned", "--entrypoint", "/bin/true", "image"]
    (receipts / "planned.json").write_text(json.dumps({"args": args, "image": "image"}))
    spec = {"Id": "owned-id", "Name": "/owned", "Image": "image", "Mounts": [],
            "Config": {"Labels": {}, "Cmd": [], "Entrypoint": ["/bin/true"]},
            "State": {"Running": False}}
    docker = tmp_path / "docker"
    docker.write_text(f'''#!{sys.executable}
import json, os, sys
if sys.argv[1] == "ps":
    print("owned-id")
elif sys.argv[1] == "inspect":
    print(json.dumps([{spec!r}]))
elif sys.argv[1:] == ["rm", "--volumes", "owned-id"]:
    with open({str(ready)!r}, "w") as stream:
        stream.write(str(os.getpid()) + "\\n")
        stream.flush()
    with open({str(release)!r}, "rb", buffering=0) as stream:
        assert stream.read(1) == b"x"
else:
    raise AssertionError(sys.argv)
''')
    docker.chmod(0o700)
    controller = tmp_path / "controller.py"
    repo = Path(__file__).resolve().parents[1]
    lock = tmp_path / "lock"
    controller.write_text(f'''import fcntl, sys
from pathlib import Path
sys.path.insert(0, {str(repo)!r})
from scripts import lab_recovery as recovery
with open({str(lock)!r}, "a+b") as stream:
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with recovery.controller_lock(stream.fileno()):
        recovery.settle_resources(Path({str(attempt)!r}))
''')
    child_pid = None
    process = subprocess.Popen([sys.executable, str(controller)],
                               env={**os.environ, "PATH": str(tmp_path)},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert select.select([ready_fd], [], [], 10)[0], "removal client did not become ready"
        child_pid = int(os.read(ready_fd, 64))
        process.kill()
        process.wait(timeout=10)
        with lock.open("a+b") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(release_fd, b"x")
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < deadline, "flock not released after client exit"
                    time.sleep(0.01)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(ready_fd)
        os.close(release_fd)
