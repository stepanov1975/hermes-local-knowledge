from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hermes_local_knowledge.config import Config


# Optional private orchestration uses flock, /proc, statvfs, directory fsync,
# inherited descriptors and POSIX permissions. Exclude before module imports;
# a runtime skip is too late for fcntl. Keep portable lab and all plugin tests.
_LINUX_ONLY_LAB_TESTS = frozenset({
    "test_lab_agent.py",
    "test_lab_automation.py",
    "test_lab_benchmark.py",
    "test_lab_candidate_bounds.py",
    "test_lab_experiment.py",
    "test_lab_lifecycle.py",
    "test_lab_notifications.py",
    "test_lab_process_output.py",
    "test_lab_recovery.py",
    "test_lab_recovery_regressions.py",
    "test_lab_recovery_successor.py",
    "test_lab_retention.py",
    "test_lab_review_evidence.py",
})


def pytest_ignore_collect(collection_path: Path) -> bool | None:
    if (sys.platform != "linux"
            and collection_path.parent == Path(__file__).resolve().parent
            and collection_path.name in _LINUX_ONLY_LAB_TESTS):
        return True
    return None


@pytest.fixture
def official_host_manager(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    # Both releases consult the public home override; scoped releases also
    # capture the home at construction. Never discover the real user's plugins.
    monkeypatch.setenv("HERMES_HOME", str(cfg.hermes_home))
    constants = importlib.import_module("hermes_constants")
    token = constants.set_hermes_home_override(cfg.hermes_home)
    manager = None
    try:
        host = importlib.import_module("hermes_cli.plugins")
        parameters = inspect.signature(host.PluginManager).parameters
        manager = (host.PluginManager(scope_key=str(cfg.hermes_home))
                   if "scope_key" in parameters else host.PluginManager())
        assert constants.get_hermes_home().resolve() == cfg.hermes_home.resolve()
        if "scope_key" in parameters:
            assert manager.home_path.resolve() == cfg.hermes_home.resolve()
        # Use real discovery before manual registration: late discovery can
        # overwrite registrations, and must not borrow process-global config.
        manager.discover_and_load()
        # Route native middleware lookup to this fixture's real manager only;
        # constructor, discovery, registration and dispatch remain unmodified.
        monkeypatch.setattr(host, "get_plugin_manager", lambda: manager)
        yield manager
    finally:
        try:
            unload = getattr(manager, "unload", None)
            if callable(unload):
                unload()
        finally:
            constants.reset_hermes_home_override(token)
