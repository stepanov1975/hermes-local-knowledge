from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_command(command: list[str], *, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"command failed: {' '.join(command)}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    return result


def test_hermes_plugin_install_register_and_search_smoke(tmp_path: Path) -> None:
    if shutil.which("hermes") is None:
        if os.environ.get("CI"):
            pytest.fail("hermes CLI is required for CI plugin install smoke")
        pytest.skip("hermes CLI is not available")

    hermes_home = tmp_path / "hermes_home"
    source_root = tmp_path / "source_root"
    state_dir = hermes_home / "local_knowledge"
    hermes_home.mkdir()
    write(
        source_root / "custom_skills" / "note-taking" / "demo-local" / "SKILL.md",
        """---
name: demo-local
description: Demo local reusable knowledge router skill.
tags: [demo, reusable]
---
# Demo Local
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "LOCAL_KNOWLEDGE_ROOT": str(source_root),
            "LOCAL_KNOWLEDGE_STATE_DIR": str(state_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    run_command(["hermes", "plugins", "install", f"file://{REPO_ROOT}", "--enable"], env=env)
    listing = run_command(["hermes", "plugins", "list", "--user", "--json"], env=env)
    plugins = json.loads(listing.stdout)
    assert any(item["name"] == "local_knowledge" and item["status"] == "enabled" for item in plugins)

    smoke_script = tmp_path / "load_and_search.py"
    write(
        smoke_script,
        textwrap.dedent(
            """
            import json
            from hermes_cli.plugins import PluginManager
            from tools.registry import registry

            manager = PluginManager()
            manager.discover_and_load(force=True)
            entry = registry.get_entry("knowledge_search")
            assert entry is not None, "knowledge_search was not registered"
            payload = json.loads(entry.handler({"query": "demo reusable", "rebuild": True}, session_id="pytest-smoke"))
            print(json.dumps({
                "tool": entry.name,
                "toolset": entry.toolset,
                "success": payload["success"],
                "rebuilt": payload["rebuilt"],
                "ids": [row["id"] for row in payload["results"]],
                "root": payload["root"],
                "state_dir": payload["state_dir"],
            }, sort_keys=True))
            """
        ).strip()
        + "\n",
    )
    result = run_command([sys.executable, str(smoke_script)], env=env)
    payload = json.loads(result.stdout)

    assert payload["tool"] == "knowledge_search"
    assert payload["toolset"] == "local_knowledge"
    assert payload["success"] is True
    assert payload["rebuilt"] is True
    assert "skill:demo-local" in payload["ids"]
    assert payload["root"] == str(source_root.resolve())
    assert payload["state_dir"] == str(state_dir.resolve())
    assert (state_dir / "index.sqlite").exists()
    assert (state_dir / "usage.sqlite").exists()
    assert not (source_root / "knowledge" / "index.sqlite").exists()
