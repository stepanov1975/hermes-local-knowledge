from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_local_knowledge import index, plugin, refresh
from hermes_local_knowledge.config import Config, IndexSettings, resolve_config
from hermes_local_knowledge.service import LocalKnowledgeService


def config(tmp_path: Path) -> Config:
    root, home = tmp_path / "source", tmp_path / "home"
    root.mkdir(parents=True)
    home.mkdir()
    (root / "alpha.md").write_text("# Alpha operations\nExisting reference.")
    return Config(root, home, tmp_path / "state", IndexSettings())


def stale(cfg: Config) -> None:
    with sqlite3.connect(cfg.state_dir / "index.sqlite") as connection:
        connection.execute("UPDATE metadata SET value='2000-01-01T00:00:00Z' WHERE key='built_at'")


def wait(cfg: Config) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with refresh._MUTEX:
            if cfg.state_dir not in refresh._RUNNING:
                return
        time.sleep(0.01)
    pytest.fail("refresh did not finish")


def test_missing_fresh_disabled_and_edit_discovery(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    assert not refresh.maybe_refresh(cfg)
    service = LocalKnowledgeService(cfg)
    assert service.ensure_index()[1]["rebuilt"]
    assert not refresh.maybe_refresh(cfg)
    (cfg.source_root / "beta.md").write_text("# Zebracobalt repair\nNew routing reference.")
    assert not service.search("zebracobalt", limit=5)[0]
    stale(cfg)
    assert not refresh.maybe_refresh(dataclasses.replace(cfg, index_max_age_seconds=0))
    assert refresh.maybe_refresh(cfg)
    wait(cfg)
    assert service.search("zebracobalt", limit=5)[0]
    assert refresh.status(cfg)["state"] == "succeeded"


def test_nonblocking_single_thread_and_locked_double_check(tmp_path: Path, monkeypatch) -> None:
    cfg = config(tmp_path)
    service = LocalKnowledgeService(cfg)
    service.rebuild()
    stale(cfg)
    calls = []
    original = index.build_index

    def build(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(index, "build_index", build)
    with index.index_build_lock(cfg.state_dir):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(lambda _: refresh.maybe_refresh(cfg), range(24)))
        assert sum(results) == 1
        with concurrent.futures.ThreadPoolExecutor() as executor:
            # A healthy managed read cannot wait for either build lock.
            rows, _ = executor.submit(service.search, "alpha", limit=5).result(timeout=2)
            assert rows
        # Simulate a competing builder publishing while the worker waits.
        original(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    wait(cfg)
    assert calls == []


def test_cross_process_double_check(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    LocalKnowledgeService(cfg).rebuild()
    stale(cfg)
    code = """
import sys
from pathlib import Path
from hermes_local_knowledge.config import Config, IndexSettings
from hermes_local_knowledge.refresh import _refresh
root = Path(sys.argv[1])
_refresh(Config(root/'source', root/'home', root/'state', IndexSettings()))
"""
    with index.index_build_lock(cfg.state_dir):
        children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)]) for _ in range(2)]
        index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
        before = (cfg.state_dir / "index.sqlite").read_bytes()
    for child in children:
        assert child.wait(timeout=10) == 0
    assert (cfg.state_dir / "index.sqlite").read_bytes() == before
    assert not refresh.status(cfg)  # neither contender built


def test_failed_build_keeps_pair_and_cross_process_cooldown(tmp_path: Path, monkeypatch) -> None:
    cfg = config(tmp_path)
    service = LocalKnowledgeService(cfg)
    service.rebuild()
    stale(cfg)
    before = [(cfg.state_dir / p).read_bytes() for p in ("index.sqlite", "index.jsonl")]

    def fail(*args, **kwargs):
        raise OSError("synthetic private path must not appear in receipt")

    monkeypatch.setattr(index, "collect_artifacts", fail)
    assert refresh.maybe_refresh(cfg)
    wait(cfg)
    assert before == [(cfg.state_dir / p).read_bytes() for p in ("index.sqlite", "index.jsonl")]
    assert service.search("alpha", limit=2)[0]
    receipt = refresh.status(cfg)
    assert receipt["state"] == "failed"
    assert receipt["error_class"] == "OSError"
    assert 0 < receipt["retry_after"] - time.time() <= refresh.RETRY_SECONDS
    assert not refresh.maybe_refresh(cfg)
    probe = """
import sys
from pathlib import Path
from hermes_local_knowledge.config import Config, IndexSettings
from hermes_local_knowledge.refresh import _due
p = Path(sys.argv[1])
assert not _due(Config(p/'source', p/'home', p/'state', IndexSettings()))
"""
    subprocess.run([sys.executable, "-c", probe, str(tmp_path)], check=True, timeout=10)
    assert "private path" not in json.dumps(receipt)
    monkeypatch.undo()
    # Explicit rebuild ignores cooldown.
    assert service.rebuild()[2]["rebuilt"]


def test_newer_and_invalid_age_do_not_refresh(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    service = LocalKnowledgeService(cfg)
    service.rebuild()
    with sqlite3.connect(service.db_path) as connection:
        connection.execute("UPDATE metadata SET value='not a timestamp' WHERE key='built_at'")
    os.utime(service.db_path, (0, 0))
    assert not refresh.maybe_refresh(cfg)
    with sqlite3.connect(service.db_path) as connection:
        connection.execute(f"PRAGMA user_version={index.INDEX_FORMAT_VERSION + 1}")
    assert not refresh.maybe_refresh(cfg)
    with pytest.raises(index.NewerIndexFormatError):
        service.ensure_index()


def test_profile_snapshot_and_hook(tmp_path: Path, monkeypatch) -> None:
    a, b = config(tmp_path / "a"), config(tmp_path / "b")
    for cfg in (a, b):
        LocalKnowledgeService(cfg).rebuild()
        stale(cfg)
    monkeypatch.setattr(plugin, "resolve_config", lambda: a)
    with index.index_build_lock(a.state_dir):
        plugin._refresh_on_activity()
        monkeypatch.setattr(plugin, "resolve_config", lambda: b)
    wait(a)
    assert refresh.status(a)["state"] == "succeeded"
    assert not refresh.status(b)
    assert refresh._due(b)


def test_config_default_and_disable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    assert resolve_config(tmp_path).index_max_age_seconds == 3600
    (tmp_path / "config.yaml").write_text("local_knowledge:\n  index_max_age_seconds: 0\n")
    assert resolve_config(tmp_path).index_max_age_seconds == 0


def test_read_serves_old_index_during_collection(tmp_path: Path, monkeypatch) -> None:
    import threading

    cfg = config(tmp_path)
    service = LocalKnowledgeService(cfg)
    service.rebuild()
    stale(cfg)
    entered, release = threading.Event(), threading.Event()
    original = index.collect_artifacts

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(index, "collect_artifacts", slow)
    assert refresh.maybe_refresh(cfg)
    try:
        assert entered.wait(3)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            assert executor.submit(service.search, "alpha", limit=5).result(timeout=2)[0]
    finally:
        release.set()
        wait(cfg)


@pytest.mark.parametrize("outcome", ["publish", "fail", "crash"])
def test_managed_reads_during_cross_process_pair_publication(tmp_path: Path, outcome: str) -> None:
    cfg = config(tmp_path)
    service = LocalKnowledgeService(cfg)
    service.rebuild()
    (cfg.source_root / "beta.md").write_text("# Zebracobalt repair\nNew reference.")
    code = """
import sys
from pathlib import Path
from hermes_local_knowledge import index
from hermes_local_knowledge.config import Config, IndexSettings
assert Path(index.__file__).resolve().is_relative_to(Path.cwd())
p = Path(sys.argv[1])
cfg = Config(p/'source', p/'home', p/'state', IndexSettings())
original = index._replace_with_retry
def paused(source, destination):
    print('publication-window', flush=True)
    assert sys.stdin.readline().strip() == 'release'
    if sys.argv[2] == 'fail':
        raise OSError('synthetic publication failure')
    original(source, destination)
index._replace_with_retry = paused
try:
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
except OSError:
    assert sys.argv[2] == 'fail'
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), outcome],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    executor = concurrent.futures.ThreadPoolExecutor()
    try:
        assert child.stdout is not None
        assert executor.submit(child.stdout.readline).result(timeout=10).strip() == "publication-window"
        assert index.index_format_state(service.db_path) == ("corrupt", index.INDEX_FORMAT_VERSION)
        old = index.search_index(service.db_path, "alpha", limit=5)
        assert old and not index.search_index(service.db_path, "zebracobalt", limit=5)
        def read():
            rows, metadata = service.search("alpha", limit=5)
            assert rows == old and not metadata["rebuilt"]
            assert service.get(old[0]["id"])[0] == index.get_artifact(service.db_path, old[0]["id"])
            assert service.neighbors(old[0]["id"])[0] == index.get_neighbors(service.db_path, old[0]["id"])
            assert not service.search("zebracobalt", limit=5)[0]
        executor.submit(read).result(timeout=2)
    finally:
        if outcome == "crash":
            child.kill()
        else:
            assert child.stdin is not None
            child.stdin.write("release\n")
            child.stdin.flush()
        child.wait(timeout=10)
        executor.shutdown(wait=True)
        if child.stdin:
            child.stdin.close()
        if child.stdout:
            child.stdout.close()
    if outcome == "crash":
        assert index._managed_index_needs_rebuild(service.db_path)
        assert service.ensure_index()[1]["rebuilt"]
    else:
        assert child.returncode == 0
    assert index.index_format_state(service.db_path) == ("current", index.INDEX_FORMAT_VERSION)
    assert bool(service.search("zebracobalt", limit=5)[0]) == (outcome != "fail")


@pytest.mark.parametrize("damage", ["sqlite", "rows", "hash", "missing", "older", "newer", "dirty"])
def test_busy_gate_never_bypasses_invalid_state(tmp_path: Path, damage: str) -> None:
    cfg = config(tmp_path)
    service = LocalKnowledgeService(cfg)
    service.rebuild()
    companion = cfg.state_dir / "index.jsonl"
    backup = cfg.state_dir / ".index.jsonl.rollback.test.tmp"
    backup.write_bytes(companion.read_bytes())
    companion.write_text("split publication")
    if damage == "sqlite":
        service.db_path.write_bytes(b"invalid sqlite")
    elif damage == "rows":
        with sqlite3.connect(service.db_path) as connection:
            connection.execute("UPDATE artifacts SET triggers_json='[null]'")
    elif damage == "hash":
        backup.write_text("wrong companion")
    elif damage == "missing":
        service.db_path.unlink()
    elif damage in {"older", "newer"}:
        version = index.INDEX_FORMAT_VERSION + (1 if damage == "newer" else -1)
        with sqlite3.connect(service.db_path) as connection:
            connection.execute(f"PRAGMA user_version={version}")
    elif damage == "dirty":
        marker = cfg.state_dir / index.DIRTY_MARKER_NAME
        marker.mkdir()
        (marker / "new-token").touch()
    with index.index_build_lock(cfg.state_dir):
        if damage == "newer":
            with pytest.raises(index.NewerIndexFormatError):
                service.ensure_index()
        else:
            if damage != "dirty":
                assert index._managed_index_needs_rebuild(service.db_path)
            assert service.ensure_index()[1]["rebuilt"]
    if damage != "newer":
        assert index.index_format_state(service.db_path) == ("current", index.INDEX_FORMAT_VERSION)
        assert not index._dirty_tokens(cfg.state_dir)


def test_launch_failure_is_optional_and_backed_off(tmp_path: Path, monkeypatch) -> None:
    cfg = config(tmp_path)
    service = LocalKnowledgeService(cfg)
    service.rebuild()
    stale(cfg)
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(refresh.threading.Thread, "start", fail)
    assert service.search("alpha", limit=5)[0]
    assert not refresh.maybe_refresh(cfg)
    assert attempts == [1]
