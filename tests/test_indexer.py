from __future__ import annotations

import json
from pathlib import Path

from hermes_local_knowledge import indexer as lci


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
        root / "main_docker_server" / "update_progress.md",
        """# Main Docker Server Update Progress

Purpose: track rolling application-update campaigns across the main Docker host
and other self-hosted applications with runbooks/update notes, so a new Hermes
session can resume without re-checking every app from scratch.

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
    assert "runbook:main-docker-server-update-progress" in artifact_ids
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
    assert update_results[0]["id"] == "runbook:main-docker-server-update-progress"

    manifest_results = lci.search_index(output_dir / "index.sqlite", "manifest-backed backup", limit=10)
    assert "runbook:main-docker-server-update-progress" in {row["id"] for row in manifest_results}

    siyuan_results = lci.search_index(output_dir / "index.sqlite", "siyuan mcp", limit=10)
    assert {row["id"] for row in siyuan_results} >= {"mcp:siyuan", "script:scripts-siyuan-mcp-run-sh"}

    hyphen_results = lci.search_index(output_dir / "index.sqlite", "paperless-review automation", limit=10)
    assert "skill:paperless-review-automation" in {row["id"] for row in hyphen_results}

    neighbor_rows = lci.get_neighbors(output_dir / "index.sqlite", "cron:paperless-reviewer")
    neighbor_ids = {row["id"] for row in neighbor_rows}
    assert "skill:paperless-review-automation" in neighbor_ids
    assert "script:scripts-paperless-review-run-reviewer-py" in neighbor_ids
    assert any(edge.source == "cron:paperless-reviewer" and edge.target == "skill:paperless-review-automation" for edge in edges)


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


def test_fts_query_splits_hyphenated_human_terms() -> None:
    assert lci.fts_query("manifest-backed backup") == "manifest* backed* backup*"
    assert lci.fts_query("self hosted application updates backup flow update markdown") == (
        "self* hosted* application* update* backup*"
    )
