from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from scripts import check_version_policy as version_policy


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def write_versions(repo: Path, version: str) -> None:
    (repo / "hermes_local_knowledge").mkdir(parents=True, exist_ok=True)
    (repo / "plugin.yaml").write_text(f"name: local_knowledge\nversion: {version}\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "hermes-local-knowledge"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (repo / "hermes_local_knowledge" / "__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )


def commit(repo: Path, message: str) -> None:
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", message)


def make_repo(tmp_path: Path, version: str = "0.1.0") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "test@example.invalid")
    run_git(repo, "config", "user.name", "Version Policy Test")
    write_versions(repo, version)
    (repo / "README.md").write_text("# test repo\n", encoding="utf-8")
    (repo / "hermes_local_knowledge" / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit(repo, "base")
    return repo


def test_release_relevant_path_classification() -> None:
    assert version_policy.is_release_relevant_path("hermes_local_knowledge/runtime.py")
    assert version_policy.is_release_relevant_path("examples/local-knowledge-router-skill/SKILL.md")
    assert version_policy.is_release_relevant_path("plugin.yaml")
    assert version_policy.is_release_relevant_path("pyproject.toml")
    assert not version_policy.is_release_relevant_path("tests/test_plugin.py")
    assert not version_policy.is_release_relevant_path("README.md")
    assert not version_policy.is_release_relevant_path(".github/workflows/ci.yml")


def test_version_bump_comparison_uses_numeric_parts() -> None:
    assert version_policy.is_version_bumped(current="0.2.10", base="0.2.9")
    assert not version_policy.is_version_bumped(current="0.2.9", base="0.2.10")
    assert not version_policy.is_version_bumped(current="0.2.9", base="0.2.9")


def test_policy_allows_docs_only_change_without_bump(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "README.md").write_text("# test repo\n\nDocs only.\n", encoding="utf-8")
    commit(repo, "docs only")

    messages = version_policy.check_version_policy(repo, base_ref="HEAD~1")

    assert "No release-relevant files changed" in messages[-1]


def test_policy_rejects_release_relevant_change_without_bump(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "hermes_local_knowledge" / "runtime.py").write_text("VALUE = 2\n", encoding="utf-8")
    commit(repo, "runtime change without bump")

    with pytest.raises(version_policy.PolicyError, match="without a plugin version bump"):
        version_policy.check_version_policy(repo, base_ref="HEAD~1")


def test_policy_allows_release_relevant_change_with_synced_bump(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "hermes_local_knowledge" / "runtime.py").write_text("VALUE = 2\n", encoding="utf-8")
    write_versions(repo, "0.1.1")
    commit(repo, "runtime change with bump")

    messages = version_policy.check_version_policy(repo, base_ref="HEAD~1")

    assert "Release-relevant files changed and version was bumped: 0.1.0 -> 0.1.1" in messages[-1]


def test_policy_includes_dirty_worktree_changes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "hermes_local_knowledge" / "runtime.py").write_text("VALUE = 2\n", encoding="utf-8")
    write_versions(repo, "0.1.1")

    messages = version_policy.check_version_policy(repo, base_ref="HEAD")

    assert "Release-relevant files changed and version was bumped: 0.1.0 -> 0.1.1" in messages[-1]


def test_policy_includes_untracked_release_relevant_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "hermes_local_knowledge" / "new_feature.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(version_policy.PolicyError, match="without a plugin version bump"):
        version_policy.check_version_policy(repo, base_ref="HEAD")


def test_policy_rejects_unsynchronized_current_metadata(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "plugin.yaml").write_text("name: local_knowledge\nversion: 0.1.1\n", encoding="utf-8")

    with pytest.raises(version_policy.PolicyError, match="not synchronized"):
        version_policy.check_version_policy(repo, base_ref=None)
