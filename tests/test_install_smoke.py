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
            "EXPECTED_PLUGIN_ROOT": str((hermes_home / "plugins" / "local_knowledge").resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env.pop("PYTHONPATH", None)

    plugin_source = tmp_path / "plugin_source"
    shutil.copytree(
        REPO_ROOT,
        plugin_source,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc", "*.egg-info"),
    )
    run_command(["git", "init", "-q"], env=env, cwd=plugin_source)
    run_command(["git", "add", "."], env=env, cwd=plugin_source)
    run_command(
        [
            "git",
            "-c",
            "user.name=Hermes Test",
            "-c",
            "user.email=hermes-test@example.invalid",
            "commit",
            "-q",
            "-m",
            "test plugin source",
        ],
        env=env,
        cwd=plugin_source,
    )
    run_command(["hermes", "plugins", "install", f"file://{plugin_source}", "--enable"], env=env)
    listing = run_command(["hermes", "plugins", "list", "--user", "--json"], env=env)
    plugins = json.loads(listing.stdout)
    assert any(item["name"] == "local_knowledge" and item["status"] == "enabled" for item in plugins)
    installed_skill = hermes_home / "plugins" / "local_knowledge" / "skills" / "local-knowledge-router" / "SKILL.md"
    assert installed_skill.exists()

    smoke_script = tmp_path / "load_and_search.py"
    write(
        smoke_script,
        textwrap.dedent(
            """
            import importlib.abc
            import importlib.metadata
            import json
            import os
            import sys
            from hermes_cli.plugins import PluginManager
            from tools.registry import registry

            def _without_packaged_local_knowledge_entrypoint(entry_points):
                rows = [
                    ep for ep in entry_points
                    if not (ep.group == "hermes_agent.plugins" and ep.name == "local_knowledge")
                ]
                try:
                    return importlib.metadata.EntryPoints(rows)
                except Exception:
                    class FilteredEntryPoints(list):
                        def select(self, **params):
                            group = params.get("group")
                            name = params.get("name")
                            return FilteredEntryPoints([
                                ep for ep in self
                                if (group is None or ep.group == group) and (name is None or ep.name == name)
                            ])
                    return FilteredEntryPoints(rows)

            # This smoke validates Hermes directory-plugin install/loading. The
            # test environment may also have this repo installed editable for
            # test dependencies, which creates the same-named pip entry point.
            # Hermes Agent 0.17 loads entry points after user plugins, so filter
            # that package entry point here or the test asserts the wrong path.
            _entry_points = importlib.metadata.entry_points
            def _entry_points_without_packaged_plugin(*args, **kwargs):
                eps = _entry_points(*args, **kwargs)
                if isinstance(eps, dict):
                    return {
                        group: _without_packaged_local_knowledge_entrypoint(group_eps)
                        if group == "hermes_agent.plugins" else group_eps
                        for group, group_eps in eps.items()
                    }
                return _without_packaged_local_knowledge_entrypoint(eps)
            importlib.metadata.entry_points = _entry_points_without_packaged_plugin

            class BlockAmbientLocalKnowledge(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "hermes_local_knowledge" or fullname.startswith("hermes_local_knowledge."):
                        raise ModuleNotFoundError(f"ambient top-level import blocked: {fullname}")
                    return None

            for name in list(sys.modules):
                if name == "hermes_local_knowledge" or name.startswith("hermes_local_knowledge."):
                    del sys.modules[name]
            sys.meta_path.insert(0, BlockAmbientLocalKnowledge())

            manager = PluginManager()
            manager.discover_and_load(force=True)
            module_files = {
                name: getattr(module, "__file__", "")
                for name, module in sys.modules.items()
                if name.startswith("hermes_plugins.") and ".hermes_local_knowledge" in name
            }
            expected_root = os.environ["EXPECTED_PLUGIN_ROOT"]
            assert module_files, "local_knowledge implementation modules were not loaded"
            assert all(path and os.path.realpath(path).startswith(expected_root) for path in module_files.values()), module_files
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
                "module_files": module_files,
            }, sort_keys=True))
            """
        ).strip()
        + "\n",
    )
    result = run_command([sys.executable, str(smoke_script)], env=env, cwd=tmp_path)
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
