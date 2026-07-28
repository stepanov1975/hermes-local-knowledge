"""Deterministic synthetic corpus for ref-isolated evaluation."""
from __future__ import annotations

import json
from pathlib import Path


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
        '"""Run staged Paperless inbox review, audit document date checks, and human-review escalation."""\nprint("ok")\n',
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
    write(
        hermes_home / "skills" / "github" / "github-workflows" / "SKILL.md",
        """---
name: github-workflows
description: Work with GitHub pull requests and review workflows.
---
# GitHub Workflows
""",
    )
    write(
        hermes_home
        / "skills"
        / "github"
        / "github-workflows"
        / "references"
        / "replacement-pr-after-stale-contributor.md",
        """# Replacement PR after stale contributor branch

Author did not reply after 24h; use this GitHub review reminder cron workflow when a replacement PR is needed.
""",
    )
    return root, hermes_home
