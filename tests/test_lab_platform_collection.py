"""Keep Linux orchestration out of unsupported hosts, not the portable plugin."""
from __future__ import annotations

import importlib
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

collection = importlib.import_module("conftest")


# Deliberately explicit: a new portable lab test must not disappear by prefix.
LINUX_ONLY = {
    "test_lab_agent.py",
    "test_lab_automation.py",
    "test_lab_benchmark.py",
    "test_lab_candidate_bounds.py",
    "test_lab_experiment.py",
    "test_lab_lifecycle.py",
    "test_lab_notifications.py",
    "test_lab_process_output.py",
    "test_lab_recovery.py",
    "test_lab_recovery_ownership.py",
    "test_lab_recovery_regressions.py",
    "test_lab_recovery_successor.py",
    "test_lab_retention.py",
    "test_lab_retention_bounds.py",
    "test_lab_review_evidence.py",
    "test_lab_workspace_bounds_lock.py",
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


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("module,expected", [
    ("test_lab_corpus.py", {
        "test_freeze_is_snapshot_not_continuing_live_review",
        "test_source_manifest_rejects_external_runtime_registry_link",
        "test_source_manifest_tracks_aliases_recursive_notes_and_drift",
    }),
    ("test_lab_evaluator.py", {
        "test_frame_noise_budget_is_cumulative_across_valid_responses",
        "test_final_stdout_drain_is_bounded",
        "test_final_stdout_preserves_buffered_and_trailing_diagnostics",
        "test_final_drain_shares_prior_read_diagnostic_allowance",
        "test_evaluator_trusted_tests_use_bounded_real_pipes",
        "test_native_transport_stderr_is_bounded_on_actual_evaluator_path",
    }),
])
def test_function_level_linux_boundaries_preserve_portable_coverage(module, expected, platform):
    tree = ast.parse((TESTS / module).read_text())
    skipped = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "skipif"):
                condition = decorator.args[0]
                assert ast.dump(condition) == ast.dump(ast.parse('sys.platform != "linux"', mode="eval").body)
                assert any(k.arg == "reason" and isinstance(k.value, ast.Constant) and k.value.value
                           for k in decorator.keywords)
                if platform != "linux":
                    skipped.add(node.name)
    assert skipped == (set() if platform == "linux" else expected)
