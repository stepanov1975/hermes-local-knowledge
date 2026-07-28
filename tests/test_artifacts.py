from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path, PureWindowsPath

import pytest

from hermes_local_knowledge import artifacts as artifacts_module
from hermes_local_knowledge.artifacts import (
    Artifact,
    Edge,
    build_edges,
    collect_artifacts,
    scan_runtime_artifacts,
    scan_skills_and_support_docs,
    scan_source_artifacts,
    scan_tool_okfs,
)
from hermes_local_knowledge.config import IndexSettings
from hermes_local_knowledge.index import build_index, get_artifact, search_index


@dataclass(frozen=True)
class Settings:
    custom_skill_dirs: tuple[str, ...] = ("custom_skills",)
    script_dirs: tuple[str, ...] = ("scripts",)
    memory_dirs: tuple[str, ...] = ("memory",)
    runbook_dirs: tuple[str, ...] = ("docs",)
    known_entities: tuple[str, ...] = ("Hermes", "GitHub", "MCP", "Cron", "Home Assistant")
    include_markdown_docs: bool = True
    exclude_dir_names: tuple[str, ...] = ()


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_complete_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Settings]:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    okf_root = tmp_path / "state" / "okfs" / "tools"
    settings = Settings()

    write(
        root / "custom_skills" / "operations" / "backup-flow" / "SKILL.md",
        """---
name: backup-flow
description: Back up Home Assistant before an upgrade.
tags: [backup, upgrade]
related_skills: [restore-flow]
---
# Backup flow
""",
    )
    write(
        root / "custom_skills" / "operations" / "backup-flow" / "references" / "restore.md",
        "# Restore guide\n\nRestore Home Assistant from the verified backup.\n",
    )
    write(
        hermes_home / "skills" / "github-workflows" / "SKILL.md",
        """---
name: github-workflows
description: Review GitHub pull requests.
---
# GitHub workflows
""",
    )
    write(
        hermes_home / "skills" / "github-workflows" / "references" / "review.md",
        "# Pull request review\n\nReview a GitHub pull request before merge.\n",
    )
    write(
        root / "scripts" / "backup.py",
        '"""Create a Home Assistant backup."""\n'
        'import os\n\n'
        'def create_backup() -> None:\n'
        '    os.getenv("HOMEASSISTANT_URL")\n'
        '    print("private-body-value")\n',
    )
    write(root / "memory" / "facts.md", "# Backup facts\n\nHome Assistant backup retention notes.\n")
    write(root / "docs" / "upgrade.md", "# Upgrade runbook\n\nBack up Home Assistant before upgrading.\n")
    write(
        root / "notes.md",
        """---
owner: local-knowledge
---
# General notes

General local routing notes.
""",
    )
    write(
        hermes_home / "cron" / "jobs.json",
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-backup",
                        "name": "nightly-backup",
                        "prompt": "Run scripts/backup.py before the Home Assistant upgrade.",
                        "schedule_display": "nightly",
                        "script": "scripts/backup.py",
                        "skills": ["backup-flow"],
                    }
                ]
            }
        ),
    )
    write(
        hermes_home / "config.yaml",
        """mcp_servers:
  github:
    command: uvx
    args: [github-mcp-server, stdio]
    env:
      GITHUB_TOKEN: not-indexed
""",
    )
    write(
        okf_root / "paperless-find.md",
        """---
artifact_type: tool_okf
tool: mcp__paperless__find_latest
toolset: paperless
schema_hash: sha256:abc123
generated_at: '2026-07-27T00:00:00Z'
title: Find latest Paperless document
aliases:
  - newest Paperless document metadata
triggers:
  - find the latest matching Paperless document
related_tools:
  - mcp__paperless__download_latest
when_not_to_use:
  - download the actual private document
---
Do not index arbitrary generated body prose.
""",
    )
    return root, hermes_home, okf_root, settings


def test_models_are_frozen_and_keep_the_public_field_contract() -> None:
    assert [field.name for field in fields(Artifact)] == [
        "id",
        "type",
        "title",
        "path",
        "summary",
        "triggers",
        "entities",
        "related",
        "updated_at",
        "source",
        "search_text",
    ]
    assert [field.name for field in fields(Edge)] == ["source", "target", "kind", "evidence"]

    artifact = Artifact("skill:backup-flow", "skill", "backup-flow", "custom_skills/backup-flow", "Backup flow")
    edge = Edge("skill:backup-flow", "runbook:upgrade", "keyword_overlap", "backup,upgrade")
    with pytest.raises(AttributeError):
        setattr(artifact, "title", "changed")
    with pytest.raises(AttributeError):
        setattr(edge, "kind", "changed")


def test_four_scanner_families_preserve_stable_ids_and_model_fields(tmp_path: Path) -> None:
    root, hermes_home, okf_root, settings = write_complete_fixture(tmp_path)

    skills = scan_skills_and_support_docs(root, hermes_home, settings)
    source = scan_source_artifacts(root, settings)
    runtime = scan_runtime_artifacts(root, hermes_home, settings)
    okfs = scan_tool_okfs(okf_root, root, settings)
    collected = collect_artifacts(root, hermes_home, settings, okf_root=okf_root)

    assert {artifact.id for artifact in skills} == {
        "skill:backup-flow",
        "skill:github-workflows",
        "skill_support_doc:custom-skills-operations-backup-flow-references-restore",
        "skill_support_doc:runtime-skills-github-workflows-references-review",
    }
    assert {artifact.id for artifact in source} == {
        "doc:notes",
        "memory_doc:memory-facts",
        "runbook:docs-upgrade",
        "script:scripts-backup-py",
    }
    assert {artifact.id for artifact in runtime} == {"cron:nightly-backup", "mcp:github"}
    assert {artifact.id for artifact in okfs} == {"tool_okf:mcp-paperless-find-latest"}
    assert [artifact.id for artifact in collected] == sorted(
        artifact.id for artifact in [*skills, *source, *runtime, *okfs]
    )

    by_id = {artifact.id: artifact for artifact in collected}
    custom_support = by_id["skill_support_doc:custom-skills-operations-backup-flow-references-restore"]
    runtime_support = by_id["skill_support_doc:runtime-skills-github-workflows-references-review"]
    assert custom_support.related == ["skill:backup-flow"]
    assert runtime_support.related == ["skill:github-workflows"]
    assert runtime_support.source == "runtime_skill_support_doc"
    assert by_id["doc:notes"].summary == "General notes"
    assert by_id["cron:nightly-backup"].related[:2] == ["skill:backup-flow", "scripts/backup.py"]
    assert by_id["mcp:github"].path.endswith("#mcp_servers.github")
    assert by_id["tool_okf:mcp-paperless-find-latest"].updated_at == "2026-07-27T00:00:00Z"


def test_optional_markdown_does_not_disable_scripts(tmp_path: Path) -> None:
    root, _hermes_home, _okf_root, settings = write_complete_fixture(tmp_path)

    artifacts = scan_source_artifacts(root, replace(settings, include_markdown_docs=False))

    assert [artifact.id for artifact in artifacts] == ["script:scripts-backup-py"]


def test_script_search_metadata_excludes_body_literals_and_values(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(
        root / "scripts" / "ha_mcp" / "run.py",
        '"""Run the Home Assistant MCP bridge."""\n'
        'import os\n\n'
        'HOMEASSISTANT_TOKEN = "script-value-canary"\n'
        'def start_bridge() -> None:\n'
        '    os.getenv("HOMEASSISTANT_URL")\n'
        '    print("body-literal-canary")\n'
        '    option = "--health-check"\n',
    )
    write(root / "scripts" / "generic" / "plain.py", "ha = object()\nprint(ha)\n")

    artifacts = scan_source_artifacts(root, Settings(include_markdown_docs=False))

    assert [artifact.id for artifact in artifacts] == [
        "script:scripts-generic-plain-py",
        "script:scripts-ha-mcp-run-py",
    ]
    by_id = {artifact.id: artifact for artifact in artifacts}
    artifact = by_id["script:scripts-ha-mcp-run-py"]
    serialized = json.dumps(asdict(artifact)).lower()
    assert "script-value-canary" not in serialized
    assert "body-literal-canary" not in serialized
    assert "homeassistant_token" in artifact.search_text.lower()
    assert "homeassistant_url" in artifact.search_text.lower()
    assert "start_bridge" in artifact.search_text.lower()
    assert "health-check" in artifact.search_text.lower()
    assert {"home", "assistant", "homeassistant", "mcp"} <= set(artifact.triggers)
    assert "assistant" not in by_id["script:scripts-generic-plain-py"].triggers
    assert "homeassistant" not in by_id["script:scripts-generic-plain-py"].triggers


def test_mcp_projection_keeps_routing_names_but_omits_credentials_and_env_values(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    write(
        hermes_home / "config.yaml",
        """mcp_servers:
  example:
    command: uvx
    url: "https://user-canary:password-canary@example.invalid/api?token=query-canary&X-Amz-Signature=signed-canary&client%5Fsecret=encoded-canary#client_secret=fragment-canary"
    args:
      - example-mcp-server
      - --token
      - argument-canary
      - --token-file
      - /home/example/token-file-canary
      - --header
      - "Cookie: session=cookie-canary"
      - --config
      - /home/example/server.json
      - MODE=stdio
    env:
      EXAMPLE_TOKEN: environment-canary
      ROUTING_MODE: private-default-canary
""",
    )

    artifacts = scan_runtime_artifacts(root, hermes_home, Settings())

    assert [artifact.id for artifact in artifacts] == ["mcp:example"]
    artifact = artifacts[0]
    serialized = json.dumps(asdict(artifact)).lower()
    for canary in (
        "user-canary",
        "password-canary",
        "argument-canary",
        "query-canary",
        "fragment-canary",
        "token-file-canary",
        "signed-canary",
        "encoded-canary",
        "cookie-canary",
        "environment-canary",
        "private-default-canary",
    ):
        assert canary not in serialized
    assert "token=<redacted>" in artifact.summary
    assert "x-amz-signature=<redacted>" in artifact.summary.lower()
    assert "client%5fsecret=<redacted>" in artifact.summary.lower()
    assert "client_secret=<redacted>" in artifact.summary
    assert "--token <redacted>" in artifact.search_text.lower()
    assert "--token-file <redacted>" in artifact.search_text.lower()
    assert "cookie: <redacted>" in artifact.search_text.lower()
    assert "example_token" in artifact.search_text.lower()
    assert "routing_mode" in artifact.search_text.lower()
    assert artifact.related == ["/home/example/server.json"]


def test_mcp_common_key_credentials_never_reach_persisted_or_public_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    state_dir = tmp_path / "state"
    root.mkdir()

    structured_names = (
        "key",
        "access_key",
        "aws_access_key_id",
        "AWS_ACCESS_KEY_ID",
        "private_key",
        "secret_key",
        "api_key",
        "api-key",
        "API_KEY",
        "apikey",
        "ssh_key",
        "consumer_key",
        "accesskeyid",
        "awsaccesskeyid",
        "sshprivatekey",
        "consumerkey",
        "signingkey",
    )
    inline_names = ("key", "accessKey", "privateKey", "secretKey", "apiKey")
    url_names = (*structured_names, *inline_names)
    env_names = ("KEY", "ACCESS_KEY", "AWS_ACCESS_KEY_ID", "PRIVATE_KEY", "SECRET_KEY", "API_KEY")

    def values(prefix: str, names: tuple[str, ...]) -> dict[str, str]:
        return {
            name: f"McpSecretCanary{prefix}{index:02d}"
            for index, name in enumerate(names, start=1)
        }

    structured = values("Structured", structured_names)
    inline = values("Inline", inline_names)
    url_parameters = values("Url", url_names)
    environment = values("Environment", env_names)
    canaries = [*structured.values(), *inline.values(), *url_parameters.values(), *environment.values()]

    structured_args = ["privacy-structured-server"]
    for index, (name, canary) in enumerate(structured.items()):
        if index % 2:
            structured_args.append(f"--{name}={canary}")
        else:
            structured_args.extend((f"--{name}", canary))
    structured_args.extend(("--keyboard", "mechanical-layout", "--keymap=vim-keymap"))

    inline_args: list[str] = ["privacy-inline-server"]
    for index, (name, canary) in enumerate(inline.items()):
        if index % 2:
            inline_args.append(f"--{name}={canary}")
        else:
            inline_args.extend((f"--{name}", canary))
    inline_args.extend(("--keyboard", "ortholinear-layout", "--keymap=colemak-keymap"))

    url_query = "&".join(f"{name}={canary}" for name, canary in url_parameters.items())
    config = {
        "mcp_servers": {
            "privacy-structured": {
                "command": "uvx",
                "args": structured_args,
            },
            "privacy-inline": {
                "command": "uvx",
                "args": f"[{', '.join(inline_args)}]",
            },
            "privacy-url-env": {
                "command": "uvx",
                "url": f"https://example.invalid/mcp?{url_query}&keyboard=ansi-layout&keymap=emacs-keymap",
                "env": environment,
            },
        }
    }
    write(hermes_home / "config.yaml", json.dumps(config))

    artifacts = scan_runtime_artifacts(root, hermes_home, IndexSettings())
    built = build_index(root, state_dir, hermes_home, IndexSettings())
    assert built is not None
    built_artifacts, _edges = built
    assert {artifact.id for artifact in artifacts} == {
        "mcp:privacy-inline",
        "mcp:privacy-structured",
        "mcp:privacy-url-env",
    }
    assert [artifact.id for artifact in built_artifacts] == sorted(artifact.id for artifact in artifacts)

    db_path = state_dir / "index.sqlite"
    public_search = search_index(db_path, "privacy mcp", limit=10)
    public_get = [get_artifact(db_path, artifact.id) for artifact in artifacts]
    text_sinks = {
        "artifact fields": json.dumps([asdict(artifact) for artifact in built_artifacts], sort_keys=True).lower(),
        "jsonl": (state_dir / "index.jsonl").read_text(encoding="utf-8").lower(),
        "public search": json.dumps(public_search, sort_keys=True).lower(),
        "public get": json.dumps(public_get, sort_keys=True).lower(),
    }
    sqlite_bytes = db_path.read_bytes().lower()
    leaks = {
        canary: [
            *[name for name, payload in text_sinks.items() if canary.lower() in payload],
            *(["sqlite"] if canary.lower().encode() in sqlite_bytes else []),
        ]
        for canary in canaries
    }
    assert {canary: sinks for canary, sinks in leaks.items() if sinks} == {}
    assert {row["id"] for row in public_search} == {artifact.id for artifact in artifacts}
    assert all(item is not None for item in public_get)

    routing_text = text_sinks["artifact fields"]
    for safe_value in (
        "mechanical-layout",
        "vim-keymap",
        "ortholinear-layout",
        "colemak-keymap",
        "ansi-layout",
        "emacs-keymap",
    ):
        assert safe_value in routing_text


def test_cron_and_mcp_related_paths_are_portable_and_exclude_url_components(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    windows_backslash = str(PureWindowsPath("C:/Users/Alex/Hermes/run.ps1"))
    windows_forward = "D:/Hermes/scripts/rebuild.cmd"
    unc = str(PureWindowsPath("//fileserver/Hermes/scripts/sync.ps1"))
    posix = "/opt/hermes/bin/worker.sh"
    home = "~/.hermes/scripts/rebuild.py"
    url = "https://example.invalid/owner's/C:/tool?next=~/run#/opt/job"
    root.mkdir()

    target_artifacts = [
        Artifact(
            id=f"script:portable-{index}",
            type="script",
            title=filename,
            path=f"scripts/{filename}",
            summary=f"Portable target {filename}.",
        )
        for index, filename in enumerate(
            ("run.ps1", "rebuild.cmd", "sync.ps1", "worker.sh", "rebuild.py"),
            start=1,
        )
    ]
    write(
        hermes_home / "cron" / "jobs.json",
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "portable-paths",
                        "name": "portable-paths",
                        "prompt": (
                            f"Run {posix}, {home}, {windows_backslash}, {windows_forward}, and {unc}. "
                            f"Ignore {url} and //example.invalid/download/C:/ignored.ps1."
                        ),
                    }
                ]
            }
        ),
    )
    write(
        hermes_home / "config.yaml",
        json.dumps(
            {
                "mcp_servers": {
                    "portable-paths": {
                        "command": "uvx",
                        "args": [
                            "portable-server",
                            "--config",
                            windows_backslash,
                            "--sync-script",
                            unc,
                            "--worker",
                            posix,
                            "--endpoint",
                            url,
                        ],
                    }
                }
            }
        ),
    )

    runtime = {artifact.id: artifact for artifact in scan_runtime_artifacts(root, hermes_home, Settings())}

    assert set(runtime["cron:portable-paths"].related) == {
        posix,
        home,
        windows_backslash,
        windows_forward,
        unc,
    }
    assert set(runtime["mcp:portable-paths"].related) == {windows_backslash, unc, posix}
    assert not {"C:/tool", "~/run", "/opt/job", "C:/ignored.ps1"} & {
        related
        for artifact in runtime.values()
        for related in artifact.related
    }

    edges = build_edges([*runtime.values(), *target_artifacts])
    targets_by_source = {
        source_id: {
            edge.target
            for edge in edges
            if edge.source == source_id and edge.kind == "related_to"
        }
        for source_id in runtime
    }
    assert targets_by_source["cron:portable-paths"] == {
        "script:portable-1",
        "script:portable-2",
        "script:portable-3",
        "script:portable-4",
        "script:portable-5",
    }
    assert targets_by_source["mcp:portable-paths"] == {
        "script:portable-1",
        "script:portable-3",
        "script:portable-4",
    }


def test_tool_okf_uses_only_positive_structured_routing_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    okf_root = tmp_path / "state" / "okfs" / "tools"
    write(
        okf_root / "cron.md",
        """---
artifact_type: tool_okf
tool: cronjob
toolset: cron
schema_hash: sha256:abc123
title: Manage scheduled jobs
aliases:
  - recurring task scheduler
triggers:
  - create or update a scheduled job
when_not_to_use:
  - retrieve a private-canary document
related_tools:
  - mcp__paperless__get_document
---
Body-prose-canary is human context, not positive routing evidence.
""",
    )
    write(okf_root / "invalid.md", "---\nartifact_type: tool_okf\ntool: missing_schema\n---\n")

    artifacts = scan_tool_okfs(okf_root, root, Settings())

    assert [artifact.id for artifact in artifacts] == ["tool_okf:cronjob"]
    artifact = artifacts[0]
    positive_text = "\n".join([artifact.search_text, *artifact.triggers, *artifact.entities]).lower()
    assert "private-canary" not in positive_text
    assert "body-prose-canary" not in positive_text
    assert artifact.related == ["tool_okf:mcp-paperless-get-document"]
    assert "recurring task scheduler" in artifact.triggers
    assert "create or update a scheduled job" in artifact.triggers


@pytest.mark.skipif(sys.platform == "win32", reason="symlink and inode behavior requires elevated Windows setup")
def test_traversal_enforces_roots_exclusions_and_inode_deduplication(tmp_path: Path) -> None:
    root = tmp_path / "worktrees" / "source"
    hermes_home = root / "worktrees" / "profile"
    external = tmp_path / "external"
    write(root / "docs" / "guide.md", "# Active guide\n\nBackup database procedure.\n")
    write(root / "build" / "ignored.md", "# Build output\n")
    write(external / "outside.md", "# Outside\n")
    write(
        hermes_home / "skills" / "active" / "SKILL.md",
        "---\nname: active\ndescription: Active runtime skill.\n---\n",
    )
    write(
        hermes_home / "skills" / ".archive" / "retired" / "SKILL.md",
        "---\nname: retired\ndescription: Archived runtime skill.\n---\n",
    )
    os.symlink(root / "docs", root / "docs-alias", target_is_directory=True)
    os.symlink(root / "docs", root / "docs" / "loop", target_is_directory=True)
    os.symlink(external, root / "docs" / "external", target_is_directory=True)
    os.link(root / "docs" / "guide.md", root / "docs" / "z-guide-hardlink.md")

    artifacts = collect_artifacts(
        root,
        hermes_home,
        Settings(exclude_dir_names=("build",)),
        okf_root=None,
    )
    ids = [artifact.id for artifact in artifacts]

    assert "runbook:docs-guide" in ids
    assert sum(artifact.type == "runbook" for artifact in artifacts) == 1
    assert "skill:active" in ids
    assert "skill:retired" not in ids
    assert not any("outside" in artifact.id or "build" in artifact.path for artifact in artifacts)


@pytest.mark.skipif(sys.platform == "win32", reason="hardlink behavior differs on Windows")
def test_distinct_configured_script_roots_preserve_stable_ids_for_one_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    source = root / "scripts" / "session_review.py"
    alias = root / "hermes_home" / "scripts" / "session_review.py"
    write(source, '"""Review session memory."""\n')
    alias.parent.mkdir(parents=True)
    os.link(source, alias)

    artifacts = collect_artifacts(
        root,
        hermes_home,
        Settings(script_dirs=("scripts", "hermes_home/scripts"), include_markdown_docs=False),
    )

    assert [artifact.id for artifact in artifacts if artifact.type == "script"] == [
        "script:hermes-home-scripts-session-review-py",
        "script:scripts-session-review-py",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation requires elevated Windows setup")
def test_collection_prefers_custom_skill_for_runtime_symlink_to_same_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    skill_dir = root / "custom_skills" / "demo"
    write(
        skill_dir / "SKILL.md",
        "---\nname: demo\ndescription: Demonstrate deterministic source de-duplication.\n---\n",
    )
    write(skill_dir / "references" / "guide.md", "# Demo guide\n")
    (hermes_home / "skills").mkdir(parents=True)
    os.symlink(skill_dir, hermes_home / "skills" / "demo", target_is_directory=True)

    artifacts = collect_artifacts(root, hermes_home, Settings(), okf_root=None)
    by_id = {artifact.id: artifact for artifact in artifacts}

    assert [artifact.id for artifact in artifacts].count("skill:demo") == 1
    assert by_id["skill:demo"].source == "custom_skill_source"
    assert [artifact.id for artifact in artifacts].count(
        "skill_support_doc:custom-skills-demo-references-guide"
    ) == 1
    assert not any(artifact.id.startswith("skill_support_doc:runtime-skills-demo") for artifact in artifacts)


def test_explicit_and_inverted_keyword_edges_are_deterministic() -> None:
    artifacts = [
        Artifact(
            id="skill:backup",
            type="skill",
            title="Database backup",
            path="custom_skills/backup",
            summary="Database backup recovery operations.",
            triggers=["database", "backup", "recovery"],
        ),
        Artifact(
            id="runbook:database-backup",
            type="runbook",
            title="Database backup runbook",
            path="docs/database-backup.md",
            summary="Database backup procedure.",
            triggers=["database", "backup"],
        ),
        Artifact(
            id="script:run-sh",
            type="script",
            title="run.sh",
            path="scripts/run.sh",
            summary="Database backup wrapper.",
            triggers=["database", "backup"],
        ),
        Artifact(
            id="cron:nightly",
            type="cron_job",
            title="Nightly",
            path="cron/jobs.json#nightly",
            summary="Nightly job.",
            related=["/opt/jobs/run.sh"],
        ),
        Artifact(
            id="skill_support_doc:backup-guide",
            type="skill_support_doc",
            title="Backup guide",
            path="custom_skills/backup/references/guide.md",
            summary="Backup guide.",
            related=["skill:backup"],
        ),
    ]

    forward = build_edges(artifacts)
    reverse = build_edges(list(reversed(artifacts)))

    assert forward == reverse
    assert forward == sorted(forward, key=lambda edge: (edge.source, edge.kind, edge.target, edge.evidence))
    assert {(edge.source, edge.target, edge.kind) for edge in forward} == {
        ("cron:nightly", "script:run-sh", "related_to"),
        ("skill:backup", "runbook:database-backup", "keyword_overlap"),
        ("skill:backup", "script:run-sh", "keyword_overlap"),
        ("skill_support_doc:backup-guide", "skill:backup", "related_to"),
    }
    overlap_evidence = {
        edge.evidence for edge in forward if edge.source == "skill:backup" and edge.kind == "keyword_overlap"
    }
    assert overlap_evidence == {"backup,database"}


@pytest.mark.parametrize("force_fallback", [False, True], ids=["yaml", "stdlib-fallback"])
def test_legacy_mcp_config_is_sanitized_with_yaml_or_fallback_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_fallback: bool,
) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    write(
        hermes_home / "config.yaml",
        """mcp:
  servers:
    github:
      command: uvx
      base_url: http://user-canary:password-canary@localhost:9000
      args: [github-mcp-server, --token, argument-canary, --config, /home/example/server.json]
      env:
        GITHUB_TOKEN: environment-canary
""",
    )
    if force_fallback:
        monkeypatch.setattr(artifacts_module, "_load_yaml", lambda _text: None)

    artifacts = scan_runtime_artifacts(root, hermes_home, Settings())

    assert [artifact.id for artifact in artifacts] == ["mcp:github"]
    artifact = artifacts[0]
    serialized = json.dumps(asdict(artifact)).lower()
    assert artifact.path.endswith("#mcp.servers.github")
    assert "url http://localhost:9000" in artifact.summary
    assert "github-mcp-server" in artifact.summary
    assert "--token <redacted>" in artifact.summary.lower()
    assert artifact.related == ["/home/example/server.json"]
    for canary in ("user-canary", "password-canary", "argument-canary", "environment-canary"):
        assert canary not in serialized


def test_mcp_config_read_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    write(
        hermes_home / "config.yaml",
        ("x" * 200_000) + "\nmcp_servers:\n  beyond-bound:\n    command: uvx\n",
    )

    assert scan_runtime_artifacts(root, hermes_home, Settings()) == []


def test_cron_registry_legacy_and_missing_name_shapes_remain_product_inputs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    registry = hermes_home / "cron" / "jobs.json"
    write(registry, json.dumps({}))
    assert scan_runtime_artifacts(root, hermes_home, Settings()) == []

    write(
        registry,
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
    [legacy] = scan_runtime_artifacts(root, hermes_home, Settings())
    assert legacy.id == "cron:nightly-backup"
    assert legacy.related == ["skill:backup-flow", "scripts/backup.py"]

    write(
        registry,
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "daily-review",
                        "prompt": "Run ~/bin/review.py and summarize changed artifacts.",
                        "schedule_display": "every 2h",
                        "script": "~/bin/review.py",
                        "skills": ["review-flow"],
                        "enabled_toolsets": ["terminal"],
                        "state": "scheduled",
                        "last_status": "ok",
                    }
                ]
            }
        ),
    )
    [current] = scan_runtime_artifacts(root, hermes_home, Settings())
    assert current.id == "cron:daily-review"
    assert current.title == "daily-review"
    assert current.path.endswith("#daily-review")
    assert current.related == ["skill:review-flow", "~/bin/review.py"]
    assert "Schedule: every 2h." in current.summary
    assert "State: scheduled." in current.summary
    assert "Last status: ok." in current.summary
    assert "terminal" in current.triggers


def test_nested_tool_okf_is_not_duplicated_as_source_markdown(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes-home"
    okf_root = root / "local_knowledge" / "okfs" / "tools"
    write(
        okf_root / "cron.md",
        """---
artifact_type: tool_okf
tool: cronjob
toolset: cron
schema_hash: sha256:abc123
title: Manage scheduled jobs
aliases: [recurring task scheduler]
triggers: [create a scheduled job]
---
Human context only.
""",
    )

    artifacts = collect_artifacts(root, hermes_home, Settings(), okf_root=okf_root)

    assert [artifact.id for artifact in artifacts] == ["tool_okf:cronjob"]
