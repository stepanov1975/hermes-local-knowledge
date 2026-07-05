from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import hermes_local_knowledge
from hermes_local_knowledge import cli as lci_cli
from hermes_local_knowledge import indexer as lci
from hermes_local_knowledge import scanners as lci_scanners
from hermes_local_knowledge import storage as lci_storage


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"

    write(
        root / "custom_skills" / "mcp" / "paperless-review-automation" / "SKILL.md",
        """---
name: paperless-review-automation
description: Operate the local Paperless review automation and OCR quality checks.
metadata:
  hermes:
    tags: [paperless, review, ocr]
    related_skills: [paperless-mcp-server]
---

# Paperless Review Automation

Use this when triaging Paperless inbox documents.
""",
    )
    write(
        root / "custom_skills" / "mcp" / "paperless-mcp-server" / "SKILL.md",
        """---
name: paperless-mcp-server
description: Build the reusable Paperless MCP server and helper tools for reviewer automation.
metadata:
  hermes:
    tags: [paperless, mcp, reviewer]
---

# Paperless MCP Server
""",
    )
    write(
        root / "scripts" / "paperless_review" / "run_reviewer.py",
        '"""Run staged Paperless inbox review and write audit logs."""\nprint("ok")\n',
    )
    write(
        root / "scripts" / "siyuan_mcp" / "run.sh",
        "#!/usr/bin/env bash\n# Launch the SiYuan MCP wrapper for Hermes.\n",
    )
    write(
        root / "memory" / "paperless_memory.md",
        "# Paperless memory\n\nPaperless reviewer facts and document workflow preferences.\n",
    )
    write(
        root / "docs" / "paperless-review-flow.md",
        "# Paperless review flow\n\nDocuments move through OCR quality guards before metadata updates.\n",
    )
    write(
        root / "docs" / "update-progress.md",
        """# Service Update Progress

Purpose: track rolling application-update campaigns across services with
runbooks/update notes, so a new Hermes session can resume without re-checking
every app from scratch.

For apps that need an app-specific artifact, dry-run the manifest-backed backup
first before mutating services. Create a verified pre-update backup during the
maintenance window.
""",
    )
    write(
        hermes_home / "cron" / "jobs.json",
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job123",
                        "name": "paperless-reviewer",
                        "prompt": f"Run {root / 'scripts' / 'paperless_review' / 'run_reviewer.py'} and report audit results.",
                        "skills": ["paperless-review-automation"],
                        "script": "run_reviewer.py",
                        "schedule_display": "every 120m",
                        "state": "scheduled",
                        "last_status": "ok",
                    }
                ]
            }
        ),
    )
    write(
        hermes_home / "config.yaml",
        f"""mcp:
  servers:
    siyuan:
      command: {root / 'scripts' / 'siyuan_mcp' / 'run.sh'}
""",
    )
    return root, hermes_home


def test_build_index_writes_searchable_artifacts_and_edges(tmp_path: Path) -> None:
    root, hermes_home = build_fixture(tmp_path)
    output_dir = tmp_path / "state"

    artifacts, edges = lci.build_index(root, output_dir, hermes_home)
    artifact_ids = {artifact.id for artifact in artifacts}

    assert "skill:paperless-review-automation" in artifact_ids
    assert "script:scripts-paperless-review-run-reviewer-py" in artifact_ids
    assert "memory_doc:memory-paperless-memory" in artifact_ids
    assert "runbook:docs-paperless-review-flow" in artifact_ids
    assert "runbook:docs-update-progress" in artifact_ids
    assert "cron:paperless-reviewer" in artifact_ids
    assert "mcp:siyuan" in artifact_ids
    assert output_dir.joinpath("index.sqlite").exists()
    assert output_dir.joinpath("index.jsonl").exists()
    assert not root.joinpath("knowledge", "index.sqlite").exists()

    search_results = lci.search_index(output_dir / "index.sqlite", "paperless review", limit=10)
    result_ids = {row["id"] for row in search_results}
    assert search_results[0]["id"] == "skill:paperless-review-automation"
    assert "skill:paperless-review-automation" in result_ids
    assert "script:scripts-paperless-review-run-reviewer-py" in result_ids

    update_results = lci.search_index(
        output_dir / "index.sqlite",
        "self hosted application updates backup flow update markdown",
        limit=10,
    )
    assert update_results[0]["id"] == "runbook:docs-update-progress"

    manifest_results = lci.search_index(output_dir / "index.sqlite", "manifest-backed backup", limit=10)
    assert "runbook:docs-update-progress" in {row["id"] for row in manifest_results}

    siyuan_results = lci.search_index(output_dir / "index.sqlite", "siyuan mcp", limit=10)
    assert {row["id"] for row in siyuan_results} >= {"mcp:siyuan", "script:scripts-siyuan-mcp-run-sh"}

    hyphen_results = lci.search_index(output_dir / "index.sqlite", "paperless-review automation", limit=10)
    assert "skill:paperless-review-automation" in {row["id"] for row in hyphen_results}

    neighbor_rows = lci.get_neighbors(output_dir / "index.sqlite", "cron:paperless-reviewer")
    neighbor_ids = {row["id"] for row in neighbor_rows}
    assert "skill:paperless-review-automation" in neighbor_ids
    assert "script:scripts-paperless-review-run-reviewer-py" in neighbor_ids
    assert any(edge.source == "cron:paperless-reviewer" and edge.target == "skill:paperless-review-automation" for edge in edges)


def test_indexer_build_index_honors_compatibility_monkeypatches(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def fake_collect(root: Path, hermes_home: Path, settings: lci.IndexSettings | None = None) -> list[lci.Artifact]:
        calls.append(f"collect:{root.name}:{hermes_home.name}:{settings is None}")
        return []

    def fake_edges(artifacts: list[lci.Artifact]) -> list[lci.Edge]:
        calls.append(f"edges:{len(artifacts)}")
        return []

    monkeypatch.setattr(lci, "collect_artifacts", fake_collect)
    monkeypatch.setattr(lci, "build_edges", fake_edges)

    artifacts, edges = lci.build_index(tmp_path / "root", tmp_path / "state", tmp_path / "hermes_home")

    assert artifacts == []
    assert edges == []
    assert calls == ["collect:root:hermes_home:True", "edges:0"]
    assert (tmp_path / "state" / "index.jsonl").exists()
    assert (tmp_path / "state" / "index.sqlite").exists()


def test_indexer_main_honors_compatibility_build_index_monkeypatch(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[Path, Path, Path]] = []

    def fake_build(root: Path, output_dir: Path, hermes_home: Path, settings=None):  # type: ignore[no-untyped-def]
        calls.append((root, output_dir, hermes_home))
        return [], []

    monkeypatch.setattr(lci, "build_index", fake_build)

    rc = lci.main(
        [
            "build",
            "--root",
            str(tmp_path / "root"),
            "--hermes-home",
            str(tmp_path / "hermes_home"),
            "--output-dir",
            str(tmp_path / "state"),
        ]
    )

    assert rc == 0
    assert calls == [(tmp_path / "root", tmp_path / "state", tmp_path / "hermes_home")]
    assert "Built 0 artifacts and 0 edges" in capsys.readouterr().out


def test_default_known_entities_are_portable() -> None:
    assert set(lci.DEFAULT_KNOWN_ENTITIES) == {"Hermes", "GitHub", "MCP", "Cron"}
    assert len(lci.DEFAULT_KNOWN_ENTITIES) == len(set(lci.DEFAULT_KNOWN_ENTITIES))


def test_default_runbook_dirs_are_portable() -> None:
    assert lci.IndexSettings().runbook_dirs == ("docs",)


def test_get_artifact_decodes_json_fields(tmp_path: Path) -> None:
    root, hermes_home = build_fixture(tmp_path)
    output_dir = tmp_path / "state"
    lci.build_index(root, output_dir, hermes_home)

    artifact = lci.get_artifact(output_dir / "index.sqlite", "skill:paperless-review-automation")

    assert artifact is not None
    assert artifact["type"] == "skill"
    assert "paperless" in artifact["triggers"]
    assert artifact["related"] == ["skill:paperless-mcp-server"]


def test_custom_layout_and_entities_are_configurable(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    write(
        root / "my_skills" / "ops" / "backup-flow" / "SKILL.md",
        """---
name: backup-flow
description: Operate AcmeCloud backup checks.
tags: [AcmeCloud]
---
# Backup Flow
""",
    )
    write(root / "runbooks" / "acme.md", "# AcmeCloud backup runbook\n")
    settings = lci.IndexSettings(
        custom_skill_dirs=("my_skills",),
        script_dirs=("bin",),
        memory_dirs=("memory",),
        runbook_dirs=("runbooks",),
        known_entities=("AcmeCloud",),
    )
    output_dir = tmp_path / "state"

    artifacts, _edges = lci.build_index(root, output_dir, hermes_home, settings)

    by_id = {artifact.id: artifact for artifact in artifacts}
    assert "skill:backup-flow" in by_id
    assert "AcmeCloud" in by_id["skill:backup-flow"].entities
    assert "runbook:runbooks-acme" in by_id


def test_extra_runbook_dirs_are_configurable(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    write(root / "ops_runbooks" / "update_progress.md", "# Update Progress\n\nManifest-backed backup flow.\n")

    artifacts, _edges = lci.build_index(
        root,
        tmp_path / "state",
        hermes_home,
        lci.IndexSettings(runbook_dirs=("docs", "ops_runbooks")),
    )

    assert "runbook:ops-runbooks-update-progress" in {artifact.id for artifact in artifacts}


def test_configured_markdown_dirs_support_knowledge_and_nested_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(root / "knowledge" / "memory.md", "# Knowledge memory\n\nReusable local facts.\n")
    write(root / "docs" / "runbooks" / "ops.md", "# Ops runbook\n\nOperational docs.\n")
    write(root / "nested" / "skills" / "demo" / "guide.md", "# Skill support guide\n\nSupport doc.\n")
    settings = lci.IndexSettings(
        custom_skill_dirs=("nested/skills",),
        script_dirs=("bin",),
        memory_dirs=("knowledge",),
        runbook_dirs=("docs/runbooks",),
    )

    artifacts = lci.scan_markdown_docs(root, settings)

    by_id = {artifact.id: artifact for artifact in artifacts}
    assert by_id["memory_doc:knowledge-memory"].type == "memory_doc"
    assert by_id["runbook:docs-runbooks-ops"].type == "runbook"
    assert by_id["skill_support_doc:nested-skills-demo-guide"].type == "skill_support_doc"


def test_followlink_scanner_prunes_cycles_and_external_targets(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    root = tmp_path / "repo"
    external = tmp_path / "external"
    write(root / "scripts" / "inside.py", '"""Inside root."""\n')
    write(external / "secret.py", '"""Outside root."""\n')
    os.symlink(root / "scripts", root / "scripts" / "loop")
    os.symlink(external, root / "scripts" / "external")

    paths = list(lci.iter_files_followlinks(root / "scripts", suffixes={".py"}, allowed_roots=(root,)))
    rel_paths = {path.relative_to(root).as_posix() for path in paths}

    assert rel_paths == {"scripts/inside.py"}


def test_markdown_scanner_prunes_cycles_and_external_targets(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    root = tmp_path / "repo"
    external = tmp_path / "external"
    write(root / "docs" / "inside.md", "# Inside\n\nVisible runbook.\n")
    write(root / "a" / "placeholder.txt", "placeholder\n")
    write(root / ".git" / "secret.md", "# Git Secret\n\nExcluded even through symlinks.\n")
    write(external / "secret.md", "# Secret\n\nOutside root.\n")
    os.symlink(root / "docs", root / "docs" / "loop")
    os.symlink(root / "docs", root / "docs-alias")
    os.symlink(root / "docs", root / "a" / "docs-link")
    os.symlink(root / ".git", root / "visible-git")
    os.symlink(external, root / "docs" / "external")
    os.symlink(external / "secret.md", root / "docs" / "linked-secret.md")

    artifacts = lci.scan_markdown_docs(root)

    assert [artifact.id for artifact in artifacts] == ["runbook:docs-inside"]


def test_exclude_dir_names_skips_configured_directories(tmp_path: Path) -> None:
    """User-supplied exclude_dir_names in IndexSettings skip matching directories."""
    root = tmp_path / "repo"
    write(root / "docs" / "visible.md", "# Visible\n\nIndexed normally.\n")
    write(root / "worktrees" / "feature-x" / "duplicated.md", "# Duplicated\n\nShould be skipped.\n")
    write(root / "build" / "output.md", "# Build Output\n\nShould be skipped.\n")

    settings = lci.IndexSettings(exclude_dir_names=("build",))
    artifacts = lci.scan_markdown_docs(root, settings)
    ids = [artifact.id for artifact in artifacts]

    # "worktrees" is excluded by the built-in defaults now
    assert "runbook:docs-visible" in ids
    assert not any("worktrees" in a.path for a in artifacts)
    assert not any("build" in a.path for a in artifacts)


def test_exclude_dir_names_do_not_skip_source_root_ancestors(tmp_path: Path) -> None:
    """Excluded names apply within the source root, not to its parent path."""
    root = tmp_path / "build" / "repo"
    write(root / "docs" / "visible.md", "# Visible\n\nIndexed even though an ancestor is named build.\n")

    artifacts = lci.scan_markdown_docs(root, lci.IndexSettings(exclude_dir_names=("build",)))

    assert [(artifact.id, artifact.path) for artifact in artifacts] == [
        ("runbook:docs-visible", "docs/visible.md")
    ]


def test_default_worktree_excludes_do_not_skip_explicit_worktree_source_root(tmp_path: Path) -> None:
    """A configured source root under worktrees/ should still be indexable."""
    root = tmp_path / "worktrees" / "feature-repo"
    write(root / "docs" / "intentional.md", "# Intentional Source Root\n\nThis checkout was configured directly.\n")

    artifacts = lci.scan_markdown_docs(root)

    assert [(artifact.id, artifact.path) for artifact in artifacts] == [
        ("runbook:docs-intentional", "docs/intentional.md")
    ]


def test_skill_support_file_excludes_are_relative_to_skill_dir(tmp_path: Path) -> None:
    """Support-file exclusions should not match source-root ancestor names."""
    skill_dir = tmp_path / "worktrees" / "feature-repo" / "custom_skills" / "demo"
    write(skill_dir / "references" / "guide.md", "# Guide\n")
    write(skill_dir / "references" / "build" / "ignored.md", "# Build output\n")

    names = lci.skill_support_file_names(skill_dir, excluded_dir_names=("build",))

    assert names == ["references/guide.md"]


def test_runtime_skills_under_excluded_source_root_segment_are_indexed(tmp_path: Path) -> None:
    """The narrower HERMES_HOME allowed root should win over a broader source root."""
    root = tmp_path / "repo"
    hermes_home = root / "worktrees" / "profile"
    write(
        hermes_home / "skills" / "runtime-demo" / "SKILL.md",
        """---
name: runtime-demo
description: Runtime demo skill.
---
# Runtime demo
""",
    )

    artifacts = lci.scan_skills(root, hermes_home)

    assert [(artifact.id, artifact.source, artifact.path) for artifact in artifacts] == [
        ("skill:runtime-demo", "runtime_skill", "worktrees/profile/skills/runtime-demo")
    ]


def test_skill_support_file_names_uses_pruned_walker(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Support-file enumeration should prune excluded dirs before descent."""
    skill_dir = tmp_path / "skill"
    write(skill_dir / "references" / "guide.md", "# Guide\n")
    calls: list[tuple[Path, tuple[Path, ...], bool, tuple[str, ...] | None]] = []

    def fake_iter_files_followlinks(
        root: Path,
        *,
        allowed_roots: tuple[Path, ...],
        followlinks: bool,
        excluded_dir_names: tuple[str, ...] | None,
        **_kwargs: object,
    ):
        calls.append((root, allowed_roots, followlinks, excluded_dir_names))
        return [skill_dir / "references" / "guide.md"] if root.name == "references" else []

    monkeypatch.setattr(lci_scanners, "iter_files_followlinks", fake_iter_files_followlinks)

    names = lci.skill_support_file_names(skill_dir, excluded_dir_names=("build",))

    assert names == ["references/guide.md"]
    assert calls == [
        (skill_dir / "references", (skill_dir,), False, ("build",)),
    ]


def test_default_excluded_dir_names_includes_worktrees() -> None:
    """The built-in EXCLUDED_DIR_NAMES must include worktrees and .worktrees."""
    from hermes_local_knowledge.constants import EXCLUDED_DIR_NAMES

    assert "worktrees" in EXCLUDED_DIR_NAMES
    assert ".worktrees" in EXCLUDED_DIR_NAMES


def test_build_sqlite_preserves_existing_db_when_rebuild_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    output_dir = tmp_path / "state"
    lci.build_index(root, output_dir, hermes_home)
    db_path = output_dir / "index.sqlite"
    before = db_path.read_bytes()

    def fail_connect(_path: str) -> sqlite3.Connection:
        raise RuntimeError("simulated sqlite failure")

    monkeypatch.setattr(lci_storage.sqlite3, "connect", fail_connect)

    with pytest.raises(RuntimeError, match="simulated sqlite failure"):
        lci.build_sqlite(db_path, [], [])

    assert db_path.read_bytes() == before


def test_build_sqlite_creates_nested_parent_directories(tmp_path: Path) -> None:
    db_path = tmp_path / "missing" / "nested" / "state" / "index.sqlite"
    artifact = lci.Artifact(
        id="skill:alpha",
        type="skill",
        title="Alpha",
        path="custom_skills/alpha",
        summary="Alpha summary",
        triggers=["alpha"],
        entities=["Hermes"],
        related=[],
        source="test",
        search_text="Alpha summary",
    )

    lci.build_sqlite(db_path, [artifact], [])

    assert db_path.exists()
    fetched = lci.get_artifact(db_path, "skill:alpha")
    assert fetched is not None
    assert fetched["title"] == "Alpha"
    assert fetched["triggers"] == ["alpha"]


def test_scan_mcp_servers_supports_native_top_level_config(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    write(
        hermes_home / "config.yaml",
        """mcp_servers:
  siyuan:
    command: /tmp/siyuan-mcp/run.sh
""",
    )

    artifacts = lci.scan_mcp_servers(root, hermes_home)

    assert [artifact.id for artifact in artifacts] == ["mcp:siyuan"]
    assert artifacts[0].path.endswith("#mcp_servers.siyuan")


def test_scan_mcp_servers_reads_config_with_size_bound(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    max_chars_seen: list[int | None] = []

    def fake_safe_read_text(path: Path, *, max_chars: int | None = None) -> str:
        max_chars_seen.append(max_chars)
        assert path == hermes_home / "config.yaml"
        return "mcp_servers:\n  github:\n    command: uvx\n"

    monkeypatch.setattr(lci_scanners, "safe_read_text", fake_safe_read_text)
    monkeypatch.setattr(
        lci_scanners,
        "load_yaml_if_available",
        lambda _path: {"mcp_servers": {"github": {"command": "uvx"}}},
    )

    artifacts = lci.scan_mcp_servers(root, hermes_home)

    assert max_chars_seen == [200_000]
    assert [artifact.id for artifact in artifacts] == ["mcp:github"]


def test_scan_mcp_servers_preserves_legacy_yaml_path_and_base_url(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    write(
        hermes_home / "config.yaml",
        """mcp:
  servers:
    github:
      command: uvx
      base_url: http://localhost:9000
      args: [github-mcp-server, stdio]
      env:
        GITHUB_TOKEN: secret-name
""",
    )

    artifacts = lci.scan_mcp_servers(root, hermes_home)

    assert [artifact.id for artifact in artifacts] == ["mcp:github"]
    artifact = artifacts[0]
    assert artifact.path.endswith("#mcp.servers.github")
    assert "url http://localhost:9000" in artifact.summary
    assert "github-mcp-server stdio" in artifact.summary
    assert "github_token" in artifact.triggers


def test_scan_mcp_servers_fallback_supports_native_top_level_config(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    write(
        hermes_home / "config.yaml",
        """mcp_servers:
  github:
    command: uvx
""",
    )
    monkeypatch.setattr(lci_scanners, "load_yaml_if_available", lambda _path: None)

    artifacts = lci.scan_mcp_servers(root, hermes_home)

    assert [artifact.id for artifact in artifacts] == ["mcp:github"]
    assert artifacts[0].path.endswith("#mcp_servers.github")


def test_scan_mcp_servers_fallback_supports_legacy_mcp_servers_config(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    write(
        hermes_home / "config.yaml",
        """mcp:
  servers:
    github:
      command: uvx
""",
    )
    monkeypatch.setattr(lci_scanners, "load_yaml_if_available", lambda _path: None)

    artifacts = lci.scan_mcp_servers(root, hermes_home)

    assert [artifact.id for artifact in artifacts] == ["mcp:github"]
    assert artifacts[0].path.endswith("#mcp.servers.github")


def test_scan_cron_jobs_handles_empty_registry_dict(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    write(hermes_home / "cron" / "jobs.json", json.dumps({}))

    assert lci.scan_cron_jobs(root, hermes_home) == []


def test_scan_cron_jobs_supports_legacy_list_payload(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    write(
        hermes_home / "cron" / "jobs.json",
        json.dumps(
            [
                "ignored",
                {
                    "id": "job1",
                    "name": "nightly-backup",
                    "prompt": "Run scripts/backup.py before updates.",
                    "schedule": "0 3 * * *",
                    "script": "scripts/backup.py",
                    "skills": ["backup-flow"],
                },
            ]
        ),
    )

    artifacts = lci.scan_cron_jobs(root, hermes_home)

    assert [artifact.id for artifact in artifacts] == ["cron:nightly-backup"]
    assert artifacts[0].related == ["skill:backup-flow", "scripts/backup.py"]


def test_scan_cron_jobs_uses_id_when_name_missing_and_preserves_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    write(
        hermes_home / "cron" / "jobs.json",
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "daily-review",
                        "prompt": "Run ~/bin/review.py and summarize changed artifacts.",
                        "schedule_display": "every 2h",
                        "schedule": "0 */2 * * *",
                        "script": "~/bin/review.py",
                        "skills": ["review-flow"],
                        "enabled_toolsets": ["terminal"],
                        "state": "scheduled",
                        "last_status": "ok",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        ),
    )

    artifacts = lci.scan_cron_jobs(root, hermes_home)

    assert [artifact.id for artifact in artifacts] == ["cron:daily-review"]
    artifact = artifacts[0]
    assert artifact.title == "daily-review"
    assert artifact.path.endswith("#daily-review")
    assert "Schedule: every 2h" in artifact.summary
    assert "State: scheduled" in artifact.summary
    assert "Last status: ok" in artifact.summary
    assert artifact.related == ["skill:review-flow", "~/bin/review.py"]
    assert artifact.updated_at == "2026-01-01T00:00:00Z"
    assert "terminal" in artifact.triggers


def test_cli_build_default_output_dir_uses_hermes_home_not_source_root(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    default_state_dir = hermes_home / "local_knowledge"

    assert lci.main(["build", "--root", str(root), "--hermes-home", str(hermes_home)]) == 0
    build_out = capsys.readouterr().out

    assert str(default_state_dir / "index.sqlite") in build_out
    assert (default_state_dir / "index.sqlite").exists()
    assert (default_state_dir / "index.jsonl").exists()
    assert not (root / "knowledge" / "index.sqlite").exists()

def test_cli_build_and_search_json(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    output_dir = tmp_path / "state"

    assert lci.main(["build", "--root", str(root), "--hermes-home", str(hermes_home), "--output-dir", str(output_dir)]) == 0
    build_out = capsys.readouterr().out
    assert "Built" in build_out
    assert "cron_job" in build_out

    assert lci.main(["search", "paperless review", "--db", str(output_dir / "index.sqlite"), "--json"]) == 0
    search_out = capsys.readouterr().out
    rows = json.loads(search_out)
    assert any(row["id"] == "skill:paperless-review-automation" for row in rows)


def test_cli_build_search_get_and_neighbors_e2e_human_output(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    output_dir = tmp_path / "state"
    db_path = output_dir / "index.sqlite"

    assert lci.main(["build", "--root", str(root), "--hermes-home", str(hermes_home), "--output-dir", str(output_dir)]) == 0
    capsys.readouterr()

    assert lci.main(["search", "paperless review", "--db", str(db_path), "--limit", "2"]) == 0
    search_out = capsys.readouterr().out
    assert "skill:paperless-review-automation [skill]" in search_out
    assert "triggers:" in search_out

    assert lci.main(["get", "skill:paperless-review-automation", "--db", str(db_path)]) == 0
    get_out = capsys.readouterr().out
    assert "summary: Operate the local Paperless review automation" in get_out

    assert lci.main(["get", "skill:paperless-review-automation", "--db", str(db_path), "--json"]) == 0
    get_payload = json.loads(capsys.readouterr().out)
    assert get_payload["id"] == "skill:paperless-review-automation"

    assert lci.main(["neighbors", "cron:paperless-reviewer", "--db", str(db_path)]) == 0
    neighbors_out = capsys.readouterr().out
    assert "skill:paperless-review-automation [skill]" in neighbors_out
    assert "edge: related_to" in neighbors_out

    assert lci.main(["neighbors", "cron:paperless-reviewer", "--db", str(db_path), "--json"]) == 0
    neighbors_payload = json.loads(capsys.readouterr().out)
    assert any(row["id"] == "skill:paperless-review-automation" for row in neighbors_payload)


def test_cli_get_missing_artifact_exits_nonzero(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    output_dir = tmp_path / "state"
    assert lci.main(["build", "--root", str(root), "--hermes-home", str(hermes_home), "--output-dir", str(output_dir)]) == 0
    capsys.readouterr()

    assert lci.main(["get", "skill:nope", "--db", str(output_dir / "index.sqlite")]) == 1
    captured = capsys.readouterr()
    assert "Artifact not found: skill:nope" in captured.err


def test_cli_build_from_hermes_config_uses_configured_layout(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    state_dir = tmp_path / "configured_state"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {state_dir}
  custom_skill_dirs:
    - custom_skills
  script_dirs:
    - scripts
  include_markdown_docs: true
""",
    )

    assert lci.main(["build", "--from-hermes-config", "--hermes-home", str(hermes_home)]) == 0
    build_out = capsys.readouterr().out

    assert str(state_dir / "index.sqlite") in build_out
    assert (state_dir / "index.sqlite").exists()
    assert not (hermes_home / "local_knowledge" / "index.sqlite").exists()


def test_cli_build_from_hermes_config_honors_output_dir_override(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    configured_state = tmp_path / "configured_state"
    override_state = tmp_path / "override_state"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {configured_state}
""",
    )

    assert lci.main(["build", "--from-hermes-config", "--hermes-home", str(hermes_home), "--output-dir", str(override_state)]) == 0
    build_out = capsys.readouterr().out

    assert str(override_state / "index.sqlite") in build_out
    assert (override_state / "index.sqlite").exists()
    assert not (configured_state / "index.sqlite").exists()


def test_cli_root_override_enables_markdown_docs_when_config_root_is_unset(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "source_root"
    hermes_home = tmp_path / "hermes_home"
    state_dir = tmp_path / "configured_state"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(root / "docs" / "backup.md", "# Backup Runbook\n\nUse this backup runbook before service updates.\n")
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  state_dir: {state_dir}
""",
    )

    assert lci.main(["build", "--from-hermes-config", "--hermes-home", str(hermes_home), "--root", str(root)]) == 0
    build_out = capsys.readouterr().out

    assert "runbook: 1" in build_out
    assert (state_dir / "index.sqlite").exists()


def test_cli_search_from_hermes_config_uses_configured_state_dir(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    state_dir = tmp_path / "configured_state"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {state_dir}
""",
    )
    assert lci.main(["build", "--from-hermes-config", "--hermes-home", str(hermes_home)]) == 0
    capsys.readouterr()

    assert lci.main(["search", "paperless review", "--from-hermes-config", "--hermes-home", str(hermes_home), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)

    assert any(row["id"] == "skill:paperless-review-automation" for row in rows)


def test_cli_commands_record_usage_telemetry_from_config(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    state_dir = tmp_path / "configured_state"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {state_dir}
""",
    )

    assert lci.main(["build", "--from-hermes-config", "--hermes-home", str(hermes_home)]) == 0
    capsys.readouterr()
    assert lci.main(["search", "paperless review", "--from-hermes-config", "--hermes-home", str(hermes_home)]) == 0
    capsys.readouterr()
    assert lci.main(["get", "skill:paperless-review-automation", "--from-hermes-config", "--hermes-home", str(hermes_home)]) == 0
    capsys.readouterr()
    assert lci.main(["neighbors", "cron:paperless-reviewer", "--from-hermes-config", "--hermes-home", str(hermes_home)]) == 0
    capsys.readouterr()
    assert lci.main(["doctor", "--hermes-home", str(hermes_home), "--query", "paperless review"]) == 0
    capsys.readouterr()

    conn = sqlite3.connect(state_dir / "usage.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in conn.execute("SELECT * FROM usage_events ORDER BY id").fetchall()]
    finally:
        conn.close()

    tools = [row["tool"] for row in rows]
    assert tools == ["cli_build", "knowledge_search", "knowledge_get", "knowledge_neighbors", "cli_doctor"]
    assert {row["client"] for row in rows} == {"cli"}
    build_row = rows[0]
    assert build_row["plugin_version"] == hermes_local_knowledge.__version__
    assert build_row["source_root_source"] == "config"
    assert build_row["state_dir_source"] == "config"
    assert build_row["rebuilt"] == 1
    assert build_row["index_artifact_count"] >= 7
    assert json.loads(build_row["index_artifact_counts_json"])["skill"] == 2
    assert build_row["build_duration_ms"] is not None
    search_row = rows[1]
    assert search_row["query"] == "paperless review"
    assert search_row["result_count"] > 0
    assert search_row["index_mtime"] is not None
    doctor_row = rows[-1]
    assert doctor_row["tool"] == "cli_doctor"
    assert doctor_row["result_count"] > 0


def test_cli_doctor_warns_when_defaulting_to_broad_hermes_home(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    (hermes_home / "hermes-agent").mkdir(parents=True)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)

    assert lci.main(["doctor", "--hermes-home", str(hermes_home)]) == 0
    captured = capsys.readouterr()

    assert "local_knowledge.source_root is unset" in captured.err
    assert str(hermes_home) in captured.out


def test_cli_doctor_can_rebuild_and_smoke_search_from_config(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    state_dir = tmp_path / "configured_state"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {state_dir}
""",
    )

    assert lci.main(["doctor", "--hermes-home", str(hermes_home), "--rebuild", "--query", "paperless review"]) == 0
    doctor_out = capsys.readouterr().out

    assert "Built" in doctor_out
    assert "Smoke query 'paperless review':" in doctor_out
    assert (state_dir / "index.sqlite").exists()


def test_cli_doctor_reports_rebuild_failure_with_context(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    state_dir = tmp_path / "configured_state"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {state_dir}
""",
    )

    def raising_build(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    status = lci_cli.main(
        ["doctor", "--hermes-home", str(hermes_home), "--rebuild", "--json"],
        build_index_fn=raising_build,
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert payload["source_root"] == str(root.resolve())
    rebuild_checks = [check for check in payload["checks"] if check["name"] == "rebuild_failed"]
    assert rebuild_checks
    assert rebuild_checks[0]["ok"] is False
    assert rebuild_checks[0]["fatal"] is True
    assert "RuntimeError: boom" in rebuild_checks[0]["detail"]


def test_cli_doctor_skips_smoke_query_after_fatal_path_check(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    missing_root = tmp_path / "missing-root"
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {missing_root}
""",
    )

    status = lci.main(["doctor", "--hermes-home", str(hermes_home), "--query", "anything", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert any(check["name"] == "source_root_exists" and check["ok"] is False for check in payload["checks"])
    assert "smoke query skipped because an earlier doctor check failed" in payload["warnings"]


def test_cli_doctor_reports_missing_index_for_smoke_query(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    state_dir = tmp_path / "state"
    root.mkdir()
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {state_dir}
""",
    )

    status = lci.main(["doctor", "--hermes-home", str(hermes_home), "--query", "anything", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert any(check["name"] == "smoke_query_index_exists" and check["ok"] is False for check in payload["checks"])
    assert any("rerun with --rebuild" in warning for warning in payload["warnings"])


def test_cli_doctor_preserves_context_when_smoke_search_fails(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    state_dir = tmp_path / "configured_state"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {root}
  state_dir: {state_dir}
""",
    )
    state_dir.mkdir(parents=True)
    (state_dir / "index.sqlite").write_text("not a real sqlite db", encoding="utf-8")

    def raising_search(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    status = lci_cli.main(
        ["doctor", "--hermes-home", str(hermes_home), "--query", "paperless review", "--json"],
        search_index_fn=raising_search,
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert payload["hermes_home"] == str(hermes_home.resolve())
    assert payload["source_root"] == str(root.resolve())
    assert any(check["name"] == "smoke_search_failed" for check in payload["checks"])


def test_fts_query_splits_hyphenated_human_terms() -> None:
    assert lci.fts_query("manifest-backed backup") == "manifest* backed* backup*"
    assert lci.fts_query("paperless review", operator="OR") == "paperless* OR review*"
    assert lci.fts_query("self hosted application updates backup flow update markdown") == (
        "self* hosted* application* update* backup*"
    )


def test_search_sort_key_scores_each_ranking_tier() -> None:
    row = {
        "id": "skill:paperless-review",
        "title": "Paperless Review",
        "path": "custom_skills/paperless-review",
        "triggers": ["paperless", "review", "automation"],
        "summary": "Paperless review automation helper.",
        "type": "skill",
        "rank": 7.5,
    }

    assert lci.search_sort_key(row, ["paperless", "review"]) == (
        0,
        -2,
        -2,
        -2,
        -2,
        0,
        7.5,
        "Paperless Review",
    )

    id_weight_row = {
        "id": "skill:paperless",
        "title": "Review",
        "path": "custom_skills/paperless",
        "triggers": [],
        "summary": "",
        "type": "skill",
        "rank": 0,
    }
    assert lci.search_sort_key(id_weight_row, ["paperless", "review"])[:2] == (0, -2)
