"""Activity-driven index maintenance; no timers, source watchers or model calls."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import index
from .config import Config

_LOG = logging.getLogger(__name__)
_MUTEX = threading.Lock()
_RUNNING: set[Path] = set()
_RETRY_UNTIL: dict[Path, float] = {}
RETRY_SECONDS = 300
_STATUS_NAME = "index_refresh.json"


def status(config: Config) -> dict[str, Any]:
    """Small operator receipt, not a job queue or a freshness authority."""
    try:
        value = json.loads((config.state_dir / _STATUS_NAME).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_status(config: Config, value: dict[str, Any]) -> None:
    path = config.state_dir / _STATUS_NAME
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _due(config: Config) -> bool:
    if config.index_max_age_seconds <= 0:
        return False
    db = config.state_dir / "index.sqlite"
    # Only successful-build metadata can establish age. Missing/unreadable state
    # belongs to the normal managed repair path, not this optional maintenance.
    try:
        connection = sqlite3.connect(index.sqlite_readonly_uri(db), uri=True, timeout=0.05)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != index.INDEX_FORMAT_VERSION:
                return False
            row = connection.execute("SELECT value FROM metadata WHERE key='built_at'").fetchone()
        finally:
            connection.close()
        if row is None or not isinstance(row[0], str):
            return False
        built = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if built.tzinfo is None:
            return False
        age = time.time() - built.timestamp()
        retry_at = float(status(config).get("retry_after", 0))
        return age >= config.index_max_age_seconds and time.time() >= retry_at
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return False


def _refresh(config: Config) -> None:
    # Both cross-process locks are reentrant in this thread. The age check MUST
    # occur after acquiring them: another process may already have refreshed.
    with index.index_build_lock(config.state_dir):
        if not _due(config):
            return
        try:
            index.build_index(config.source_root, config.state_dir, config.hermes_home,
                              config.index_settings, force=True)
        except Exception as exc:
            _write_status(config, {"state": "failed", "error_class": type(exc).__name__,
                                   "retry_after": time.time() + RETRY_SECONDS})
            raise
        _write_status(config, {"state": "succeeded", "completed_at": time.time(),
                               "retry_after": 0})


def _run(config: Config, key: Path) -> None:
    try:
        _refresh(config)
    except Exception as exc:
        _LOG.warning("local_knowledge index refresh failed (%s)", type(exc).__name__)
        with _MUTEX:
            _RETRY_UNTIL[key] = time.monotonic() + RETRY_SECONDS
    finally:
        with _MUTEX:
            _RUNNING.discard(key)


def maybe_refresh(config: Config) -> bool:
    """Check metadata and start at most one daemon thread per process/index.

    Config is immutable and captured here, never resolved in the worker. Short
    lived CLI processes may exit before maintenance completes; explicit builds
    are the reliable synchronous alternative. Existing publication recovery owns
    interruption safety.
    """
    key = config.state_dir.resolve()
    with _MUTEX:
        for expired in list(_RETRY_UNTIL):
            if _RETRY_UNTIL[expired] <= time.monotonic():
                del _RETRY_UNTIL[expired]
        if key in _RUNNING or key in _RETRY_UNTIL or not _due(config):
            return False
        _RUNNING.add(key)
        try:
            threading.Thread(target=_run, args=(config, key),
                             name="lk-index-refresh", daemon=True).start()
        except Exception as exc:
            _RUNNING.discard(key)
            _RETRY_UNTIL[key] = time.monotonic() + RETRY_SECONDS
            _LOG.warning("local_knowledge index refresh launch failed (%s)", type(exc).__name__)
            return False
    return True
