from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from hermes_local_knowledge.artifacts import _script_summary, scan_skills_and_support_docs
from hermes_local_knowledge.config import IndexSettings
from hermes_local_knowledge.index import build_index, search_index


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"""Module summary.\nMore detail."""\nvalue = 1\n', "Module summary. More detail."),
        ('#!/usr/bin/env python3\n# coding: utf-8\nu"Module summary."\n', "Module summary."),
        ('r"""Module summary."""\n', "Module summary."),
        ('("Module " "summary.")\n', "Module summary."),
        ('value = 1\n"""Not a module docstring."""\n', "Local script example.py"),
        ('b"""Not a text docstring."""\n', "Local script example.py"),
        ('f"""Not a constant docstring."""\n', "Local script example.py"),
        ('# Header summary.\nvalue = """assigned\n"""\n', "Header summary."),
        ('# Header summary.\n"""Truncated string', "Header summary."),
        ('def broken(:\n', "Local script example.py"),
    ],
)
def test_python_script_summary_uses_only_module_docstring(text: str, expected: str) -> None:
    assert _script_summary(Path("example.py"), text) == expected


def test_non_python_summary_does_not_interpret_python_quotes() -> None:
    text = '#!/bin/sh\n# Shell header.\ncat <<\'EOF\'\n"""not documentation"""\nEOF\n'
    assert _script_summary(Path("example.sh"), text) == "Shell header."


def test_assigned_multiline_strings_do_not_supply_script_summary(tmp_path: Path) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    write(
        root / "scripts" / "export.py",
        'query = """\nSELECT 1\n"""\n'
        'API_TOKEN = "SYNTHETIC_CREDENTIAL_CANARY"\n'
        'other = """harmless\n"""\n',
    )
    artifacts, _ = build_index(
        root, state, tmp_path / "hermes", IndexSettings(include_markdown_docs=False)
    )
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.summary == "Local script export.py"
    assert "SYNTHETIC_CREDENTIAL_CANARY" not in json.dumps(asdict(artifact))
    assert "SYNTHETIC_CREDENTIAL_CANARY" not in (state / "index.jsonl").read_text()
    assert search_index(state / "index.sqlite", '"SYNTHETIC_CREDENTIAL_CANARY"') == []
    result = search_index(state / "index.sqlite", "export", artifact_type="script")
    assert [row["id"] for row in result] == ["script:scripts-export-py"]
    assert result[0]["summary"] == "Local script export.py"


@pytest.mark.parametrize("root_layout", ["same", "ancestor", "separate"])
@pytest.mark.parametrize("include_markdown_docs", [False, True])
def test_runtime_support_scanner_does_not_skip_source_root_descendants(
    tmp_path: Path, root_layout: str, include_markdown_docs: bool
) -> None:
    root = tmp_path / "source"
    home = root if root_layout == "same" else (
        root / "runtime" if root_layout == "ancestor" else tmp_path / "hermes"
    )
    write(home / "skills" / "backup" / "SKILL.md", "---\nname: backup\ndescription: Back up files.\n---\n")
    support = home / "skills" / "backup" / "references" / "restore.md"
    write(support, "# Restore guide\nRecover the quasar checkpoint.\n")
    write(home / "skills" / ".archive" / "old" / "SKILL.md", "---\nname: old\n---\n")
    write(home / "skills" / ".archive" / "old" / "references" / "old.md", "# Retired guide\n")
    settings = IndexSettings(include_markdown_docs=include_markdown_docs)
    artifacts = scan_skills_and_support_docs(root, home, settings)
    assert [artifact.id for artifact in artifacts] == [
        "skill:backup", "skill_support_doc:runtime-skills-backup-references-restore"
    ]
    assert artifacts[1].related == ["skill:backup"]
    assert artifacts[1].path == str(support)
    assert artifacts[1].source == "runtime_skill_support_doc"
    state = tmp_path / "state"
    built, _ = build_index(root, state, home, settings)
    assert [artifact.id for artifact in built] == [artifact.id for artifact in artifacts]
    results = search_index(state / "index.sqlite", '"quasar checkpoint"', artifact_type="skill_support_doc")
    assert [row["id"] for row in results] == [artifacts[1].id]


@pytest.mark.parametrize("include_markdown_docs", [False, True])
def test_runtime_custom_support_overlap_deduplicates_only_collected_sources(
    tmp_path: Path, include_markdown_docs: bool
) -> None:
    home = tmp_path / "hermes"
    write(home / "skills" / "backup" / "SKILL.md", "---\nname: backup\n---\n")
    write(home / "skills" / "backup" / "references" / "restore.md", "# Restore guide\n")
    artifacts = scan_skills_and_support_docs(
        home, home,
        IndexSettings(custom_skill_dirs=("skills",), include_markdown_docs=include_markdown_docs),
    )
    support = [artifact for artifact in artifacts if artifact.type == "skill_support_doc"]
    assert len(support) == 1
    expected = "skills-backup" if include_markdown_docs else "runtime-skills-backup"
    assert support[0].id == f"skill_support_doc:{expected}-references-restore"
    assert support[0].related == ["skill:backup"]
