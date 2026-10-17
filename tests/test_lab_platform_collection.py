"""Keep Linux orchestration out of unsupported hosts, not the portable plugin."""
from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

collection = importlib.import_module("conftest")


# Deliberately explicit: a new portable lab test must not disappear by prefix.
LINUX_ONLY = {
    "test_lab_agent.py",
    "test_lab_automation.py",
    "test_lab_benchmark.py",
    "test_lab_experiment.py",
    "test_lab_lifecycle.py",
    "test_lab_notifications.py",
    "test_lab_process_output.py",
    "test_lab_recovery.py",
    "test_lab_recovery_regressions.py",
    "test_lab_retention.py",
    "test_lab_review_evidence.py",
}
TESTS = Path(__file__).resolve().parent


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_lab_collection_boundary(platform: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace this conftest's reference, never pytest/pathlib's live sys.platform.
    monkeypatch.setattr(collection, "sys", SimpleNamespace(platform=platform))
    modules = set(TESTS.glob("test_*.py"))
    ignored = {path.name for path in modules if collection.pytest_ignore_collect(path)}
    assert ignored == (set() if platform == "linux" else LINUX_ONLY)


@pytest.mark.parametrize("name", [
    "test_plugin.py", "test_public_contract.py", "test_lab_platform_collection.py",
    "test_lab_corpus.py", "test_lab_evaluator.py", "test_lab_model_client.py",
    "test_lab_runtime.py", "test_lab_future_portable.py",
])
def test_windows_keeps_portable_tests(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collection, "sys", SimpleNamespace(platform="win32"))
    assert not collection.pytest_ignore_collect(TESTS / name)


def test_exclusion_is_scoped_to_this_tests_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collection, "sys", SimpleNamespace(platform="win32"))
    assert not collection.pytest_ignore_collect(TESTS / "nested" / "test_lab_agent.py")
    assert not collection.pytest_ignore_collect(TESTS / "test_lab_agent.py.backup")
    assert not collection.pytest_ignore_collect(TESTS)
