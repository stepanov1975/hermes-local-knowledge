from __future__ import annotations

import importlib
import inspect
import json
import sqlite3
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import hermes_local_knowledge
import pytest
from hermes_local_knowledge import cli as lci_cli
from hermes_local_knowledge import index as lci_index
from hermes_local_knowledge import okf
from hermes_local_knowledge import plugin
from hermes_local_knowledge import telemetry as lci_telemetry
from hermes_local_knowledge.artifacts import Artifact
from hermes_local_knowledge.config import Config, resolve_config
from hermes_local_knowledge.routing import (
    ROUTING_TRACE_METADATA_KEY,
    RouteDecision,
    RouteOutcome,
    SearchRoutingTrace,
)
from hermes_local_knowledge.service import LocalKnowledgeService


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_version_metadata_stays_in_sync():
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    plugin_version = next(
        line.partition(":")[2].strip()
        for line in (repo_root / "plugin.yaml").read_text(encoding="utf-8").splitlines()
        if line.startswith("version:")
    )

    assert hermes_local_knowledge.__version__ == "0.4.11"
    assert hermes_local_knowledge.__version__ == pyproject["project"]["version"]
    assert hermes_local_knowledge.__version__ == plugin_version


def test_packaging_discovery_excludes_mutation_workspace():
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    find_config = pyproject["tool"]["setuptools"]["packages"]["find"]
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert find_config["include"] == ["hermes_local_knowledge*"]
    assert "mutants*" in find_config["exclude"]
    assert package_data["hermes_local_knowledge"] == ["skills/*/SKILL.md"]


def make_temp_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    state_dir = tmp_path / "state"
    (repo / "scripts").mkdir(parents=True)
    (repo / "custom_skills" / "note-taking" / "paperless-review-automation").mkdir(parents=True)
    (repo / "custom_skills" / "note-taking" / "paperless-helper").mkdir(parents=True)
    hermes_home.mkdir()

    write(
        repo / "scripts" / "paperless_review_helper.py",
        """#!/usr/bin/env python3
\"\"\"Paperless review helper script for visual review automation.\"\"\"
""",
    )
    write(
        repo / "custom_skills" / "note-taking" / "paperless-review-automation" / "SKILL.md",
        """---
name: paperless-review-automation
description: Operate Paperless visual review automation and reviewer cron.
tags:
  - Paperless
  - review
related_skills:
  - paperless-helper
---
# Paperless review automation
""",
    )
    write(
        repo / "custom_skills" / "note-taking" / "paperless-helper" / "SKILL.md",
        """---
name: paperless-helper
description: Supporting Paperless helper procedures.
tags:
  - Paperless
---
# Paperless helper
""",
    )
    return repo, hermes_home, state_dir


def configure_env(monkeypatch, repo: Path, hermes_home: Path, state_dir: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LOCAL_KNOWLEDGE_ROOT", str(repo))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


def test_register_exposes_native_tools_and_bundled_skill():
    tool_calls = []
    skill_calls = []
    cli_calls = []
    hook_calls = []
    host_llm = object()

    class Ctx:
        llm = host_llm

        def register_tool(self, **kwargs):
            tool_calls.append(kwargs)

        def register_skill(self, name, skill_md):  # type: ignore[no-untyped-def]
            skill_calls.append((name, Path(skill_md)))

        def register_cli_command(self, **kwargs):  # type: ignore[no-untyped-def]
            cli_calls.append(kwargs)

        def register_hook(self, name, callback):  # type: ignore[no-untyped-def]
            hook_calls.append((name, callback))

    plugin.register(Ctx())

    assert plugin.__all__ == ["register"]
    assert [call["name"] for call in tool_calls] == [
        "knowledge_search",
        "knowledge_get",
        "knowledge_neighbors",
        "knowledge_feedback",
        "knowledge_usage_report",
    ]
    assert {call["toolset"] for call in tool_calls} == {"local_knowledge"}
    assert all(call["schema"]["parameters"]["type"] == "object" for call in tool_calls)
    assert all(call["check_fn"] is plugin.check_knowledge_available for call in tool_calls)
    feedback_call = next(call for call in tool_calls if call["name"] == "knowledge_feedback")
    feedback_properties = feedback_call["schema"]["parameters"]["properties"]
    assert feedback_properties["expected_artifact_id"]["type"] == "string"
    assert feedback_properties["resolves_feedback_id"]["type"] == "integer"
    assert feedback_properties["resolves_feedback_id"]["minimum"] == 1
    assert "expected_artifact_id" not in feedback_call["schema"]["parameters"]["required"]
    assert "resolves_feedback_id" not in feedback_call["schema"]["parameters"]["required"]
    expected_skill = Path(__file__).resolve().parents[1] / "skills" / "local-knowledge-router" / "SKILL.md"
    assert skill_calls == [("local-knowledge-router", expected_skill)]
    assert skill_calls[0][1].is_file()
    assert len(cli_calls) == 1
    assert cli_calls[0]["name"] == "local-knowledge"
    assert callable(cli_calls[0]["setup_fn"])
    assert callable(cli_calls[0]["handler_fn"])
    assert cli_calls[0]["handler_fn"].keywords["llm"] is host_llm
    assert hook_calls == [
        ("pre_llm_call", plugin._on_pre_llm_call),
        ("post_tool_call", plugin._on_post_tool_call),
        ("on_session_end", plugin._on_implicit_session_end),
        ("on_session_finalize", plugin._on_session_finalize),
    ]


def test_register_prefers_system_prompt_section_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section_calls = []
    hook_calls = []
    implicit_kwargs: dict[str, object] = {}

    def bind_implicit_context(**kwargs):  # type: ignore[no-untyped-def]
        implicit_kwargs.update(kwargs)

    monkeypatch.setattr(plugin, "_on_implicit_pre_llm_call", bind_implicit_context)
    monkeypatch.setattr(plugin, "check_knowledge_available", lambda: True)

    class Ctx:
        llm = None

        def register_tool(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def register_skill(self, _name, _skill_md):  # type: ignore[no-untyped-def]
            return None

        def register_cli_command(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def register_hook(self, name, callback):  # type: ignore[no-untyped-def]
            hook_calls.append((name, callback))

        def register_system_prompt_section(
            self,
            section_id,
            content,
            **kwargs,
        ):  # type: ignore[no-untyped-def]
            section_calls.append((section_id, content, kwargs))

    plugin.register(Ctx())

    assert section_calls == [
        (
            "local-knowledge.discovery",
            plugin._render_search_hint,
            {"position": "after_memory", "max_chars": 200},
        )
    ]
    assert section_calls[0][1]({"session_id": "session-1"}) == plugin.KNOWLEDGE_SEARCH_HINT
    assert hook_calls[0] == ("pre_llm_call", plugin._bind_implicit_pre_llm_context)
    assert hook_calls[0][1](turn_id="turn-1") is None
    assert implicit_kwargs["turn_id"] == "turn-1"

    monkeypatch.setattr(plugin, "check_knowledge_available", lambda: False)
    assert section_calls[0][1]({"session_id": "session-2"}) == ""


def test_supported_hermes_renders_search_hint_as_system_prompt_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_plugins = importlib.import_module("hermes_cli.plugins")
    plugin_context = hermes_plugins.PluginContext
    if not hasattr(plugin_context, "register_system_prompt_section"):
        pytest.skip("installed Hermes predates system-prompt sections")

    manager = hermes_plugins.PluginManager()
    manifest = hermes_plugins.PluginManifest(
        name="local_knowledge",
        key="local_knowledge",
        source="test",
    )
    ctx = plugin_context(manifest, manager)
    monkeypatch.setattr(plugin, "check_knowledge_available", lambda: True)

    plugin.register(ctx)
    rendered = manager.render_system_prompt_sections({"session_id": "session-1"})

    assert [(section.id, section.content) for section in rendered] == [
        ("local-knowledge.discovery", plugin.KNOWLEDGE_SEARCH_HINT)
    ]


def test_supported_hermes_keeps_two_profile_tool_state_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_plugins = importlib.import_module("hermes_cli.plugins")
    hermes_constants = importlib.import_module("hermes_constants")
    registry = importlib.import_module("tools.registry").registry
    if "scope_key" not in inspect.signature(hermes_plugins.PluginManager).parameters:
        pytest.skip("installed Hermes predates in-process profile-scoped plugin managers")

    default_home = tmp_path / "default"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)

    profiles: dict[str, tuple[Path, Path, str]] = {}
    managers = []
    for name, token in (("alpha", "quasaralpha"), ("beta", "nebulabeta")):
        profile_home = tmp_path / "profiles" / name
        source_root = tmp_path / "sources" / name
        write(source_root / "docs" / "capability.md", f"# {token}\n")
        write(
            profile_home / "config.yaml",
            f"""local_knowledge:
  source_root: {source_root}
""",
        )
        manager = hermes_plugins.PluginManager(scope_key=str(profile_home.resolve()))
        manifest = hermes_plugins.PluginManifest(
            name="local_knowledge",
            key="local_knowledge",
            source="test",
        )
        scope_token = hermes_constants.set_hermes_home_override(profile_home)
        try:
            plugin.register(hermes_plugins.PluginContext(manifest, manager))
        finally:
            hermes_constants.reset_hermes_home_override(scope_token)
        managers.append(manager)
        profiles[name] = (profile_home, source_root, token)

    try:
        for name, (profile_home, source_root, own_query) in profiles.items():
            other_query = profiles["beta" if name == "alpha" else "alpha"][2]
            scope_token = hermes_constants.set_hermes_home_override(profile_home)
            try:
                resolved = resolve_config()
                assert resolved.hermes_home == profile_home.resolve()
                assert resolved.source_root == source_root.resolve()
                own = json.loads(
                    registry.dispatch(
                        "knowledge_search",
                        {"query": own_query, "rebuild": True},
                        scope=str(profile_home.resolve()),
                    )
                )
                other = json.loads(
                    registry.dispatch(
                        "knowledge_search",
                        {"query": other_query},
                        scope=str(profile_home.resolve()),
                    )
                )
            finally:
                hermes_constants.reset_hermes_home_override(scope_token)

            assert own["success"] is True
            assert own["results"]
            assert other["success"] is True
            assert other["results"] == []
            assert (profile_home / "local_knowledge" / "index.sqlite").is_file()
            assert (profile_home / "local_knowledge" / "usage.sqlite").is_file()
    finally:
        for manager in managers:
            manager.unload()


def test_knowledge_availability_requires_source_and_home_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    hermes_home = tmp_path / "home"
    source_root.write_text("not a directory", encoding="utf-8")
    hermes_home.mkdir()
    monkeypatch.setattr(
        plugin,
        "resolve_config",
        lambda: SimpleNamespace(source_root=source_root, hermes_home=hermes_home),
    )
    assert plugin.check_knowledge_available() is False

    source_root.unlink()
    source_root.mkdir()
    assert plugin.check_knowledge_available() is True

    hermes_home.rmdir()
    hermes_home.write_text("not a directory", encoding="utf-8")
    assert plugin.check_knowledge_available() is False


def test_pre_llm_hook_injects_search_hint_into_real_api_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_kwargs: dict[str, object] = {}

    def bind_implicit_context(**kwargs):  # type: ignore[no-untyped-def]
        callback_kwargs.update(kwargs)

    monkeypatch.setattr(plugin, "_on_implicit_pre_llm_call", bind_implicit_context)
    monkeypatch.setattr(plugin, "check_knowledge_available", lambda: True)

    result = plugin._on_pre_llm_call(
        session_id="session-1",
        task_id="task-1",
        turn_id="turn-1",
        user_message="Where is the backup runbook?",
    )

    assert result == {"context": plugin.KNOWLEDGE_SEARCH_HINT}
    assert result is not None
    assert callback_kwargs["turn_id"] == "turn-1"

    compose_user_api_content = importlib.import_module(
        "agent.turn_context"
    ).compose_user_api_content

    api_content = compose_user_api_content(
        "Where is the backup runbook?",
        "Remembered deployment context.",
        result["context"],
    )
    assert api_content is not None
    assert api_content.startswith("Where is the backup runbook?\n\n")
    assert "Remembered deployment context." in api_content
    assert api_content.endswith(plugin.KNOWLEDGE_SEARCH_HINT)
    assert api_content.count(plugin.KNOWLEDGE_SEARCH_HINT) == 1


def test_pre_llm_hook_omits_hint_when_local_knowledge_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_kwargs: dict[str, object] = {}

    def bind_implicit_context(**kwargs):  # type: ignore[no-untyped-def]
        callback_kwargs.update(kwargs)

    monkeypatch.setattr(plugin, "_on_implicit_pre_llm_call", bind_implicit_context)
    monkeypatch.setattr(plugin, "check_knowledge_available", lambda: False)

    assert plugin._on_pre_llm_call(turn_id="turn-2") is None
    assert callback_kwargs["turn_id"] == "turn-2"


def test_pre_llm_hook_does_not_duplicate_hint_already_in_api_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_kwargs: dict[str, object] = {}

    def bind_implicit_context(**kwargs):  # type: ignore[no-untyped-def]
        callback_kwargs.update(kwargs)

    monkeypatch.setattr(plugin, "_on_implicit_pre_llm_call", bind_implicit_context)
    monkeypatch.setattr(plugin, "check_knowledge_available", lambda: True)

    history = [
        {
            "role": "user",
            "content": "Where is the backup runbook?",
            "api_content": (
                "Where is the backup runbook?\n\n" + plugin.KNOWLEDGE_SEARCH_HINT
            ),
        }
    ]
    assert (
        plugin._on_pre_llm_call(
            turn_id="turn-3",
            conversation_history=history,
        )
        is None
    )
    assert callback_kwargs["turn_id"] == "turn-3"


def test_feedback_handler_forwards_verified_fields_and_uses_atomic_usage_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeService:
        usage_db_path = tmp_path / "usage.sqlite"

        def feedback(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return 41, 42

        def record_usage(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("successful feedback telemetry must be atomic")

    monkeypatch.setattr(plugin, "_service", FakeService)

    payload = json.loads(
        plugin._handle_feedback(
            {
                "rating": "useful",
                "event_id": 7,
                "query": "  exact  internal spacing  ",
                "artifact_id": "skill:accepted",
                "expected_artifact_id": "skill:accepted",
                "resolves_feedback_id": 9,
            }
        )
    )

    assert payload["feedback_id"] == 41
    assert payload["usage_event_id"] == 42
    assert captured["expected_artifact_id"] == "skill:accepted"
    assert captured["resolves_feedback_id"] == 9
    assert isinstance(captured["usage_started_at"], float)


def test_native_search_telemetry_keeps_the_complete_bounded_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    rows = [
        {"id": f"skill:item-{position}", "type": "skill"}
        for position in range(1, 31)
    ]

    class FakeService:
        db_path = tmp_path / "index.sqlite"

        def search(self, query: str, **kwargs):  # type: ignore[no-untyped-def]
            assert query == "exact  spacing"
            assert kwargs["limit"] == 30
            return rows, {
                "rebuilt": False,
                "jsonl_sha256": "native-hash",
                "index_format_version": 4,
                ROUTING_TRACE_METADATA_KEY: SearchRoutingTrace(
                    baseline_ids=(
                        "skill:baseline",
                        *tuple(row["id"] for row in rows[1:]),
                    ),
                    decision=RouteDecision(
                        rows=rows,
                        outcome=RouteOutcome.PROMOTED_RETRY,
                        feedback_id=41,
                        artifact_id="skill:item-1",
                        feedback_max_id=44,
                    ),
                ),
            }

        def record_usage(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return 88

    monkeypatch.setattr(plugin, "_service", FakeService)

    payload = json.loads(plugin._handle_search({"query": "  exact  spacing  ", "limit": 999}))

    assert payload["limit"] == 30
    assert len(payload["results"]) == 30
    assert captured["query"] == "exact  spacing"
    assert captured["top_ids"] == [row["id"] for row in rows]
    assert captured["top_types"] == [row["type"] for row in rows]
    assert captured["baseline_top_ids"] == [
        "skill:baseline",
        *[row["id"] for row in rows[1:]],
    ]
    assert captured["route_feedback_id"] == 41
    assert captured["route_artifact_id"] == "skill:item-1"
    assert captured["route_outcome"] == "promoted_retry"
    assert captured["feedback_max_id"] == 44
    assert ROUTING_TRACE_METADATA_KEY not in payload
    index_metadata = captured["index_metadata"]
    assert isinstance(index_metadata, dict)
    assert ROUTING_TRACE_METADATA_KEY not in index_metadata


def test_cli_search_telemetry_keeps_the_complete_page_and_explicit_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    rows = [
        {"id": f"skill:item-{position}", "type": "skill"}
        for position in range(1, 51)
    ]

    class FakeService:
        def search(self, query: str, **kwargs):  # type: ignore[no-untyped-def]
            assert query == "cli query"
            assert kwargs["limit"] == 999
            return rows, {
                "rebuilt": False,
                "jsonl_sha256": "cli-hash",
                "index_format_version": 4,
                ROUTING_TRACE_METADATA_KEY: SearchRoutingTrace(
                    baseline_ids=tuple(row["id"] for row in rows),
                    decision=RouteDecision(
                        rows=rows,
                        outcome=RouteOutcome.NONE,
                        feedback_id=None,
                        artifact_id=None,
                        feedback_max_id=12,
                    ),
                ),
            }

        def record_usage(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return 89

    service = FakeService()
    monkeypatch.setattr(lci_cli, "_service", lambda *_args, **_kwargs: service)

    status = lci_cli.main(
        ["search", "cli query", "--limit", "999", "--db", str(tmp_path / "index.sqlite"), "--json"]
    )

    assert status == 0
    assert len(json.loads(capsys.readouterr().out)) == 50
    assert captured["limit_value"] == 999
    assert captured["top_ids"] == [row["id"] for row in rows]
    assert captured["top_types"] == [row["type"] for row in rows]
    assert captured["baseline_top_ids"] == [row["id"] for row in rows]
    assert captured["route_feedback_id"] is None
    assert captured["route_artifact_id"] is None
    assert captured["route_outcome"] == "none"
    assert captured["feedback_max_id"] == 12
    index_metadata = captured["index_metadata"]
    assert isinstance(index_metadata, dict)
    assert ROUTING_TRACE_METADATA_KEY not in index_metadata


def test_plugin_handler_wrapper_uses_one_service_factory(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[object, ...]] = []

    class FakeService:
        db_path = tmp_path / "state" / "index.sqlite"

        def search(
            self,
            query: str,
            *,
            limit: int,
            artifact_type: str | None,
            rebuild: bool,
        ) -> tuple[list[dict[str, object]], dict[str, object]]:
            calls.append(("search", query, limit, artifact_type, rebuild))
            return (
                [{"id": "skill:demo", "type": "skill", "title": "Demo"}],
                {"rebuilt": True, "db_path": str(self.db_path)},
            )

        def record_usage(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(("usage", kwargs))
            return 17

    service = FakeService()

    def factory() -> FakeService:
        calls.append(("factory",))
        return service

    monkeypatch.setattr(plugin, "_service", factory)

    payload = json.loads(
        plugin._handle_search(
            {"query": "demo", "limit": 2, "rebuild": True},
            session_id="session-1",
        )
    )

    assert payload["success"] is True
    assert payload["usage_event_id"] == 17
    assert payload["results"][0]["id"] == "skill:demo"
    assert calls[0:2] == [("factory",), ("search", "demo", 2, None, True)]
    assert len([call for call in calls if call[0] == "factory"]) == 1
    usage = calls[2][1]
    assert isinstance(usage, dict)
    assert usage["success"] is True
    assert usage["context"] == {
        "session_id": "session-1",
        "task_id": "",
        "tool_call_id": "",
        "api_request_id": "",
    }


def test_plugin_service_uses_resolved_config_and_core_defaults(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    service = plugin._service()

    assert isinstance(service, LocalKnowledgeService)
    assert service.config == resolve_config()
    assert service.config.source_root == repo.resolve()
    assert service.config.hermes_home == hermes_home.resolve()
    assert service.config.state_dir == state_dir.resolve()
    assert service._build_index_fn is lci_index.build_index
    assert service._search_index_fn is lci_index.search_index
    assert service._get_artifact_fn is lci_index.get_artifact
    assert service._get_neighbors_fn is lci_index.get_neighbors


def test_cli_build_delegates_forced_build_without_outer_lifecycle_ownership(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    okf.mark_index_dirty(state_dir)
    token = okf.index_dirty_tokens(state_dir)[0]
    calls: list[bool] = []

    def fake_build(
        root: Path,
        output_dir: Path,
        home: Path,
        settings,
        *,
        force: bool,
    ):  # type: ignore[no-untyped-def]
        assert (root, output_dir, home) == (repo.resolve(), state_dir.resolve(), hermes_home.resolve())
        assert settings is not None
        calls.append(force)
        return [], []

    status = lci_cli.main(
        [
            "build",
            "--root",
            str(repo),
            "--output-dir",
            str(state_dir),
            "--hermes-home",
            str(hermes_home),
        ],
        build_index_fn=fake_build,
    )

    assert status == 0
    assert calls == [True]
    assert token.is_file()
    assert not (state_dir / lci_index.INDEX_BUILD_LOCK_NAME).exists()
    assert not (state_dir / lci_index.INDEX_BUILD_TRANSACTION_LOCK_NAME).exists()


def test_cli_search_refreshes_dirty_default_index(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    lci_index.build_index(repo, state_dir, hermes_home)
    write(
        repo / "custom_skills" / "note-taking" / "late-okf-router" / "SKILL.md",
        """---
name: late-okf-router
description: Route newly completed late OKF lookups.
tags:
  - completion
---
# Late OKF router
""",
    )
    okf.mark_index_dirty(state_dir)

    status = lci_cli.main(
        [
            "search",
            "newly completed late OKF",
            "--json",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
        ]
    )
    rows = json.loads(capsys.readouterr().out)

    assert status == 0
    assert any(row["id"] == "skill:late-okf-router" for row in rows)
    assert not okf.index_dirty_tokens(state_dir)


@pytest.mark.parametrize(
    "command",
    [
        ["search", "Paperless review automation", "--json"],
        ["get", "skill:paperless-review-automation", "--json"],
        ["neighbors", "skill:paperless-review-automation", "--json"],
    ],
)
def test_cli_unconfigured_default_lookup_never_rebuilds_shared_state(
    tmp_path: Path, monkeypatch, capsys, command: list[str]
) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, _state_dir = make_temp_repo(tmp_path)
    default_state = hermes_home / "local_knowledge"
    lci_index.build_index(repo, default_state, hermes_home)
    okf.mark_index_dirty(default_state)
    unrelated = tmp_path / "unrelated-cwd"
    write(
        unrelated / "custom_skills" / "wrong-source" / "SKILL.md",
        """---
name: wrong-source
description: Artifact from an unrelated current directory.
---
# Wrong source
""",
    )
    monkeypatch.chdir(unrelated)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    build_calls: list[str] = []

    def unexpected_build(*_args):  # type: ignore[no-untyped-def]
        build_calls.append("called")
        raise AssertionError("unconfigured lookup must not rebuild shared default state")

    status = lci_cli.main(
        [*command, "--hermes-home", str(hermes_home)],
        build_index_fn=unexpected_build,
    )
    capsys.readouterr()

    assert status == 0
    assert build_calls == []
    assert len(okf.index_dirty_tokens(default_state)) == 1


def test_cli_explicit_index_sqlite_is_never_auto_rebuilt(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, _state_dir = make_temp_repo(tmp_path)
    custom_state = tmp_path / "custom-db"
    db_path = custom_state / "index.sqlite"
    lci_index.build_index(repo, custom_state, hermes_home)
    okf.mark_index_dirty(custom_state)
    build_calls: list[str] = []

    def unexpected_build(*_args):  # type: ignore[no-untyped-def]
        build_calls.append("called")
        raise AssertionError("explicit database must not be rebuilt")

    status = lci_cli.main(
        [
            "search",
            "Paperless review automation",
            "--json",
            "--db",
            str(db_path),
            "--hermes-home",
            str(hermes_home),
        ],
        build_index_fn=unexpected_build,
    )
    rows = json.loads(capsys.readouterr().out)

    assert status == 0
    assert rows
    assert build_calls == []
    assert len(okf.index_dirty_tokens(custom_state)) == 1


def test_completed_okf_is_discoverable_on_next_normal_search(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    initial = json.loads(plugin._handle_search({"query": "paperless", "rebuild": True}))
    assert initial["success"] is True

    tool_name = "mcp__paperless__paperless_find_latest_document"
    schema = {"type": "object"}
    okf.upsert_tool_candidate(
        state_dir,
        tool_name=tool_name,
        toolset="paperless",
        schema=schema,
        args={},
    )
    claimed = okf.claim_candidates(state_dir, limit=1, claim_token="claim-1")
    assert [row["tool_name"] for row in claimed] == [tool_name]
    output = okf.okf_file_path(state_dir, tool_name)
    write(
        output,
        f"""---
artifact_type: tool_okf
tool: {tool_name}
toolset: paperless
schema_hash: {okf.schema_hash(schema)}
generated_at: '2026-07-10T12:00:00Z'
aliases:
  - find newest paperless document
triggers:
  - User asks for the latest matching Paperless document metadata.
---

# Tool OKF: paperless_find_latest_document

Route to this tool for metadata about the newest matching Paperless document.
""",
    )
    assert okf.mark_candidate_done(
        state_dir,
        tool_name=tool_name,
        claim_token="claim-1",
        okf_path=output,
    )

    result = json.loads(
        plugin._handle_search(
            {
                "query": "find newest paperless document",
                "artifact_type": "tool_okf",
            }
        )
    )

    assert result["success"] is True
    assert result["rebuilt"] is True
    assert [row["id"] for row in result["results"]] == [
        "tool_okf:mcp-paperless-paperless-find-latest-document"
    ]
    assert not okf.index_dirty_tokens(state_dir)


def test_handlers_return_json_errors_for_malformed_args() -> None:
    for handler in (
        plugin._handle_search,
        plugin._handle_get,
        plugin._handle_neighbors,
        plugin._handle_feedback,
        plugin._handle_usage_report,
    ):
        payload = json.loads(handler(None))
        assert payload["success"] is False
        assert payload["error"] == "args must be an object"


def test_search_get_and_neighbors_build_missing_index_in_state_dir(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    search = json.loads(
        plugin._handle_search(
            {"query": "paperless review automation", "limit": 5, "rebuild": True}
        )
    )
    assert search["success"] is True
    assert search["rebuilt"] is True
    assert search["root"] == str(repo.resolve())
    assert search["state_dir"] == str(state_dir.resolve())
    assert isinstance(search["usage_event_id"], int)
    ids = [row["id"] for row in search["results"]]
    assert "skill:paperless-review-automation" in ids
    assert (state_dir / "index.sqlite").exists()
    assert (state_dir / "usage.sqlite").exists()
    assert not (repo / "knowledge" / "index.sqlite").exists()

    script_search = json.loads(
        plugin._handle_search(
            {"query": "paperless review automation", "limit": 5, "artifact_type": "script"}
        )
    )
    assert script_search["success"] is True
    assert {row["type"] for row in script_search["results"]} == {"script"}
    assert [row["id"] for row in script_search["results"]] == ["script:scripts-paperless-review-helper-py"]

    fetched = json.loads(
        plugin._handle_get(
            {"artifact_id": "skill:paperless-review-automation", "include_neighbors": True}
        )
    )
    assert fetched["success"] is True
    assert fetched["artifact"]["title"] == "paperless-review-automation"
    assert isinstance(fetched["usage_event_id"], int)
    neighbor_ids = {row["id"] for row in fetched["neighbors"]}
    assert "skill:paperless-helper" in neighbor_ids

    neighbors = json.loads(
        plugin._handle_neighbors({"artifact_id": "skill:paperless-review-automation"})
    )
    assert neighbors["success"] is True
    assert isinstance(neighbors["usage_event_id"], int)
    assert any(row["edge_kind"] == "related_to" for row in neighbors["neighbors"])


def test_plugin_service_environment_overrides_hermes_config_yaml(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    env_repo = tmp_path / "env_repo"
    env_state = tmp_path / "env_state"
    write(env_repo / "scripts" / "env_helper.py", '"""Environment selected helper."""\n')
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
""",
    )
    configure_env(monkeypatch, env_repo, hermes_home, env_state)

    payload = json.loads(plugin._handle_search({"query": "environment selected", "rebuild": True}))

    assert payload["success"] is True
    assert payload["root"] == str(env_repo.resolve())
    assert payload["state_dir"] == str(env_state.resolve())
    assert [row["id"] for row in payload["results"]] == ["script:scripts-env-helper-py"]


def test_handle_search_uses_one_service_and_records_usage_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    db_path = tmp_path / "state" / "index.sqlite"
    captured: dict[str, object] = {}
    build_forces: list[bool] = []
    factory_calls = 0

    def fake_build(*_args, force: bool, **_kwargs):  # type: ignore[no-untyped-def]
        build_forces.append(force)
        return [], []

    def fake_record_usage(root_arg: Path, **kwargs):  # type: ignore[no-untyped-def]
        captured["record_root"] = root_arg
        captured["record_usage_kwargs"] = kwargs
        return 123

    def fake_search(
        _db_path: Path,
        query: str,
        limit: int = 8,
        artifact_type=None,
    ):  # type: ignore[no-untyped-def]
        captured["search_artifact_type"] = artifact_type
        return [{"id": "skill:demo", "type": "skill", "title": query}]

    config = Config(
        source_root=root,
        hermes_home=tmp_path / "hermes-home",
        state_dir=db_path.parent,
        index_settings=lci_index.IndexSettings(),
    )
    service = LocalKnowledgeService(
        config,
        build_index_fn=fake_build,
        search_index_fn=fake_search,
        index_metadata_fn=lambda _path: {"index_exists": True},
        record_usage_fn=fake_record_usage,
    )

    def service_factory() -> LocalKnowledgeService:
        nonlocal factory_calls
        factory_calls += 1
        return service

    monkeypatch.setattr(plugin, "_service", service_factory)
    payload = json.loads(
        plugin._handle_search(
            {"query": "demo", "limit": 2, "rebuild": True, "artifact_type": "script"},
            session_id="session-123",
        )
    )

    assert payload["success"] is True
    assert payload["usage_event_id"] == 123
    assert payload["rebuilt"] is True
    assert factory_calls == 1
    assert build_forces == [True]
    usage_kwargs = captured["record_usage_kwargs"]
    assert isinstance(usage_kwargs, dict)
    assert usage_kwargs["context"] == {
        "session_id": "session-123",
        "task_id": "",
        "tool_call_id": "",
        "api_request_id": "",
    }
    assert usage_kwargs["query"] == "demo"
    assert usage_kwargs["artifact_type"] == "script"
    assert usage_kwargs["db_path"] == db_path
    assert captured["search_artifact_type"] == "script"
    assert captured["record_root"] == root


def test_handler_service_construction_error_stays_in_error_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_factory() -> LocalKnowledgeService:
        raise RuntimeError("service unavailable")

    monkeypatch.setattr(plugin, "_service", failing_factory)
    payload = json.loads(plugin._handle_search({"query": "demo"}))

    assert payload == {
        "error": "knowledge_search failed: RuntimeError: service unavailable",
        "success": False,
        "usage_event_id": None,
    }


def test_lookup_error_telemetry_is_fail_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        source_root=tmp_path / "repo",
        hermes_home=tmp_path / "hermes-home",
        state_dir=tmp_path / "state",
        index_settings=lci_index.IndexSettings(),
    )

    def no_build(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def failing_search(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("lookup unavailable")

    def failing_telemetry(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("telemetry unavailable")

    service = LocalKnowledgeService(
        config,
        build_index_fn=no_build,
        search_index_fn=failing_search,
        index_metadata_fn=lambda _path: {"index_exists": True},
        record_usage_fn=failing_telemetry,
    )

    monkeypatch.setattr(plugin, "_service", lambda: service)
    payload = json.loads(plugin._handle_search({"query": "demo"}))

    assert payload == {
        "error": "knowledge_search failed: RuntimeError: lookup unavailable",
        "success": False,
        "usage_event_id": None,
    }


def test_successful_search_telemetry_is_fail_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        source_root=tmp_path / "repo",
        hermes_home=tmp_path / "hermes-home",
        state_dir=tmp_path / "state",
        index_settings=lci_index.IndexSettings(),
    )

    def failing_telemetry(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("telemetry unavailable")

    service = LocalKnowledgeService(
        config,
        search_index_fn=lambda *_args, **_kwargs: [{"id": "skill:demo", "type": "skill"}],
        index_metadata_fn=lambda _path: {"index_exists": True},
        index_source_root_fn=lambda _path: str(config.source_root),
        record_usage_fn=failing_telemetry,
    )

    monkeypatch.setattr(plugin, "_service", lambda: service)
    payload = json.loads(plugin._handle_search({"query": "demo"}))

    assert payload["success"] is True
    assert payload["usage_event_id"] is None
    assert payload["results"] == [{"id": "skill:demo", "type": "skill"}]


def test_feedback_nonlock_failure_retains_best_effort_error_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        source_root=tmp_path / "repo",
        hermes_home=tmp_path / "hermes-home",
        state_dir=tmp_path / "state",
        index_settings=lci_index.IndexSettings(),
    )
    captured: dict[str, object] = {}

    def failing_feedback(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("feedback unavailable")

    def record_usage(root: Path, **kwargs):  # type: ignore[no-untyped-def]
        captured["root"] = root
        captured["kwargs"] = kwargs
        return 91

    service = LocalKnowledgeService(
        config,
        record_feedback_fn=failing_feedback,
        record_usage_fn=record_usage,
    )

    monkeypatch.setattr(plugin, "_service", lambda: service)
    payload = json.loads(
        plugin._handle_feedback(
            {"rating": "useful", "query": "demo"},
            session_id="session-91",
        )
    )

    assert payload == {
        "error": (
            "knowledge_feedback failed: OperationalError: feedback unavailable"
        ),
        "success": False,
        "usage_event_id": 91,
    }
    assert captured["root"] == config.source_root
    usage = captured["kwargs"]
    assert isinstance(usage, dict)
    assert usage["success"] is False
    assert usage["query"] == "demo"
    assert usage["context"] == {
        "session_id": "session-91",
        "task_id": "",
        "tool_call_id": "",
        "api_request_id": "",
    }
    assert usage["usage_db_path"] == service.usage_db_path


def test_feedback_lock_failure_does_not_open_a_second_telemetry_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        source_root=tmp_path / "repo",
        hermes_home=tmp_path / "hermes-home",
        state_dir=tmp_path / "state",
        index_settings=lci_index.IndexSettings(),
    )
    record_calls = 0

    def locked_feedback(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise lci_telemetry.FeedbackDatabaseLockedError(
            "feedback database is temporarily locked; try again"
        )

    def record_usage(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal record_calls
        record_calls += 1
        return 91

    service = LocalKnowledgeService(
        config,
        record_feedback_fn=locked_feedback,
        record_usage_fn=record_usage,
    )
    monkeypatch.setattr(plugin, "_service", lambda: service)

    payload = json.loads(plugin._handle_feedback({"rating": "useful", "query": "demo"}))

    assert payload == {
        "error": (
            "knowledge_feedback failed: FeedbackDatabaseLockedError: "
            "feedback database is temporarily locked; try again"
        ),
        "success": False,
        "usage_event_id": None,
    }
    assert record_calls == 0


def test_implicit_hermes_home_source_skips_root_markdown(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    write(hermes_home / "private_notes.md", "# Private Notes\n\nRoot Markdown should not be indexed implicitly.\n")
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    cfg = resolve_config()
    payload = json.loads(plugin._handle_search({"query": "private notes", "rebuild": True}))

    assert cfg.source_root == hermes_home.resolve()
    assert cfg.index_settings.include_markdown_docs is False
    assert payload["success"] is True
    assert payload["results"] == []
    assert (hermes_home / "local_knowledge" / "index.sqlite").exists()


def test_explicit_source_root_can_disable_markdown_docs(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    write(repo / "docs" / "private.md", "# Private Markdown\n\nShould be skipped when markdown docs are disabled.\n")
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
  include_markdown_docs: false
""",
    )

    payload = json.loads(plugin._handle_search({"query": "private markdown", "rebuild": True}))

    assert payload["success"] is True
    assert payload["results"] == []
    assert payload["include_markdown_docs_source"] == "config"


def test_implicit_hermes_home_source_warns_when_source_checkout_exists(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_home"
    (hermes_home / "hermes-agent").mkdir(parents=True)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    payload = json.loads(plugin._handle_search({"query": "anything", "rebuild": True}))

    assert payload["success"] is True
    assert any("local_knowledge.source_root is unset" in warning for warning in payload["warnings"])


def test_missing_artifact_returns_tool_error(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    payload = json.loads(plugin._handle_get({"artifact_id": "skill:nope", "rebuild": True}))

    assert payload["success"] is False
    assert "Artifact not found" in payload["error"]
    assert isinstance(payload["usage_event_id"], int)


def test_lookup_handlers_validate_required_fields():
    search = json.loads(plugin._handle_search({"query": ""}))
    fetched = json.loads(plugin._handle_get({"artifact_id": ""}))
    neighbors = json.loads(plugin._handle_neighbors({"artifact_id": ""}))

    assert search["success"] is False
    assert search["error"] == "query is required"
    assert fetched["success"] is False
    assert fetched["error"] == "artifact_id is required"
    assert neighbors["success"] is False
    assert neighbors["error"] == "artifact_id is required"


def test_neighbors_missing_artifact_returns_tool_error(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    payload = json.loads(plugin._handle_neighbors({"artifact_id": "skill:nope", "rebuild": True}))

    assert payload["success"] is False
    assert "Artifact not found" in payload["error"]
    assert isinstance(payload["usage_event_id"], int)


def test_empty_usage_report_before_any_lookup_returns_zero_counts(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    payload = json.loads(plugin._handle_usage_report({"days": 7, "limit": 5}))

    assert payload["success"] is True
    assert payload["total_events"] == 0
    assert payload["feedback_count"] == 0
    assert payload["improvement_candidates"] == []
    assert (state_dir / "usage.sqlite").exists()


def test_feedback_rejects_invalid_rating_and_event_id():
    invalid_rating = json.loads(plugin._handle_feedback({"rating": "great"}))
    invalid_event_id = json.loads(plugin._handle_feedback({"rating": "useful", "event_id": "abc"}))

    assert invalid_rating["success"] is False
    assert "rating must be one of" in invalid_rating["error"]
    assert invalid_event_id["success"] is False
    assert invalid_event_id["error"] == "event_id must be an integer when provided"


@pytest.mark.parametrize("index_state", ["missing", "older", "corrupt"])
@pytest.mark.parametrize("lookup", ["search", "get", "neighbors"])
def test_lookup_handlers_rebuild_noncurrent_index_in_isolation(
    tmp_path,
    monkeypatch,
    index_state: str,
    lookup: str,
):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    db_path = state_dir / "index.sqlite"
    if index_state == "older":
        stale = Artifact(
            id="tool_okf:stale-only",
            type="tool_okf",
            title="Stale only",
            path="okfs/tools/stale-only.md",
            summary="Stale only",
            triggers=["stale only"],
            search_text="stale only",
        )
        db_path.parent.mkdir(parents=True, exist_ok=True)
        lci_index._build_sqlite(
            db_path,
            [stale],
            [],
            source_root=repo,
            build_duration_ms=0,
        )
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(f"PRAGMA user_version = {lci_index.INDEX_FORMAT_VERSION - 1}")
            conn.commit()
        finally:
            conn.close()
    elif index_state == "corrupt":
        state_dir.mkdir(parents=True)
        db_path.write_text("not a sqlite db", encoding="utf-8")

    handler, args = {
        "search": (plugin._handle_search, {"query": "paperless"}),
        "get": (
            plugin._handle_get,
            {"artifact_id": "skill:paperless-review-automation"},
        ),
        "neighbors": (
            plugin._handle_neighbors,
            {"artifact_id": "skill:paperless-review-automation"},
        ),
    }[lookup]
    payload = json.loads(handler(args))

    assert payload["success"] is True
    assert lci_index.index_format_version(db_path) == lci_index.INDEX_FORMAT_VERSION
    if lookup == "search":
        assert any(row["id"] == "skill:paperless-review-automation" for row in payload["results"])
    elif lookup == "get":
        assert payload["artifact"]["id"] == "skill:paperless-review-automation"
    else:
        assert any(row["id"] == "skill:paperless-helper" for row in payload["neighbors"])


@pytest.mark.parametrize("rebuild", [False, True])
@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (plugin._handle_search, {"query": "current"}),
        (plugin._handle_get, {"artifact_id": "skill:current"}),
        (plugin._handle_neighbors, {"artifact_id": "skill:current"}),
    ],
)
def test_lookup_handlers_reject_newer_index_without_rebuild(
    tmp_path,
    monkeypatch,
    handler,
    args,
    rebuild,
):  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    db_path = state_dir / "index.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    current = lci_index.Artifact(
        id="skill:current",
        type="skill",
        title="Current",
        path="custom_skills/current/SKILL.md",
        summary="Current index",
        search_text="current index",
    )
    lci_index._build_sqlite(
        db_path,
        [current],
        [],
        source_root=repo,
        build_duration_ms=0,
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"PRAGMA user_version = {lci_index.INDEX_FORMAT_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()
    before = db_path.read_bytes()
    scan_calls: list[str] = []

    def unexpected_scan(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        scan_calls.append("scan")
        raise AssertionError("newer indexes must be refused before scanning")

    monkeypatch.setattr(lci_index, "collect_artifacts", unexpected_scan)

    payload = json.loads(handler({**args, "rebuild": rebuild}))

    assert payload["success"] is False
    assert payload["error_code"] == "newer_index_format"
    assert payload["expected_index_format_version"] == lci_index.INDEX_FORMAT_VERSION
    assert payload["actual_index_format_version"] == lci_index.INDEX_FORMAT_VERSION + 1
    assert scan_calls == []
    assert db_path.read_bytes() == before


def test_feedback_and_usage_report_close_loop(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    search = json.loads(
        plugin._handle_search({"query": "paperless review automation", "limit": 3, "rebuild": True})
    )
    zero = json.loads(plugin._handle_search({"query": "zzzzzzzz unlikely", "limit": 3}))
    assert zero["success"] is True
    assert zero["results"] == []

    feedback = json.loads(
        plugin._handle_feedback(
            {
                "event_id": search["usage_event_id"],
                "rating": "wrong_artifact",
                "artifact_id": "skill:paperless-review-automation",
                "query": "paperless review automation",
                "note": "test feedback",
            }
        )
    )
    assert feedback["success"] is True
    assert isinstance(feedback["feedback_id"], int)

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    assert report["success"] is True
    assert report["total_events"] >= 3
    assert report["feedback_count"] == 1
    assert report["live_total_events"] == report["total_events"]
    assert report["root_breakdown"][0]["root_scope"] == "live"
    assert any(row["query"] == "zzzzzzzz unlikely" for row in report["zero_result_queries"])
    assert any(row["query"] == "zzzzzzzz unlikely" for row in report["unresolved_zero_result_queries"])
    assert any(row["rating"] == "wrong_artifact" for row in report["recent_negative_feedback"])
    assert any(row["rating"] == "wrong_artifact" for row in report["live_recent_negative_feedback"])
    assert any(item["type"] == "zero_result_query" for item in report["improvement_candidates"])
    assert any(item["type"] == "feedback_wrong_artifact" for item in report["improvement_candidates"])
    assert report["latest_index_metadata"]["plugin_version"] == hermes_local_knowledge.__version__
    assert report["latest_index_metadata"]["source_root_source"] == "env"
    assert report["latest_index_metadata"]["index_artifact_count"] >= 3
    assert report["latest_index_metadata"]["index_artifact_counts"]["skill"] == 2
    assert report["recent_builds"]
    assert report["recent_builds"][0]["index_artifact_counts"]["script"] == 1


def test_usage_report_separates_roots_and_suppresses_resolved_zero_results(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    now = datetime.now(timezone.utc)

    def ts(delta: timedelta) -> str:
        return (now + delta).isoformat(timespec="seconds").replace("+00:00", "Z")

    stamps = iter(
        [
            ts(timedelta(days=-6)),
            ts(timedelta(days=-5)),
            ts(timedelta(days=-2)),
            ts(timedelta(days=-4)),
            ts(timedelta(days=-1, hours=-1)),
            ts(timedelta(hours=-3)),
            ts(timedelta(hours=-2)),
            ts(timedelta(hours=-1)),
            ts(timedelta()),
        ]
    )
    monkeypatch.setattr(lci_telemetry, "_utc_now", lambda: next(stamps))
    usage_db_path = state_dir / "usage.sqlite"

    lci_telemetry._record_usage(repo, tool="knowledge_search", success=True, query="fixed query", result_count=0)
    lci_telemetry._record_usage(repo, tool="knowledge_search", success=True, query="fixed query", result_count=2)
    lci_telemetry._record_usage(repo, tool="knowledge_search", success=True, query="still missing", result_count=0)
    lci_telemetry._record_usage(repo, tool="knowledge_search", success=False, query="old live", error="old live error")
    lci_telemetry._record_usage(repo, tool="knowledge_search", success=False, query="recent live", error="recent live error")
    lci_telemetry._record_usage(repo, tool="knowledge_search", success=True, query="XXXX", result_count=0)
    lci_telemetry._record_usage(
        Path("/tmp/pytest-of-alex/router-test/repo"),
        tool="knowledge_search",
        success=False,
        query="test failure",
        error="test root error",
        usage_db_path=usage_db_path,
    )
    lci_telemetry._record_usage(
        Path("/tmp/pytest-of-alex/router-test/repo"),
        tool="knowledge_search",
        success=True,
        query="test zero",
        result_count=0,
        usage_db_path=usage_db_path,
    )

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    scopes = {row["root_scope"]: row for row in report["root_breakdown"]}
    assert scopes["live"]["count"] == 6
    assert scopes["test_tmp"]["count"] == 2
    assert report["live_total_events"] == 6
    assert report["total_events"] == 8
    assert [row["query"] for row in report["resolved_zero_result_queries"]] == ["fixed query"]
    assert {row["query"] for row in report["unresolved_zero_result_queries"]} == {"still missing", "XXXX"}
    assert [row["query"] for row in report["active_zero_result_queries"]] == ["still missing"]
    assert [row["query"] for row in report["probe_zero_result_queries"]] == ["XXXX"]
    assert all(row["query"] != "test zero" for row in report["unresolved_zero_result_queries"])
    assert {row["error"] for row in report["live_errors"]} == {"old live error", "recent live error"}
    assert [row["error"] for row in report["recent_live_errors"]] == ["recent live error"]
    candidate_queries = {row.get("query") for row in report["improvement_candidates"]}
    candidate_errors = {row.get("error") for row in report["improvement_candidates"] if row.get("error")}
    assert "still missing" in candidate_queries
    assert "fixed query" not in candidate_queries
    assert "XXXX" not in candidate_queries
    assert "test zero" not in candidate_queries
    assert candidate_errors == {"recent live error"}


def test_usage_report_buckets_unknown_feedback_ratings(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    lci_telemetry._record_feedback(repo, rating="great", event_id=None, query="", artifact_id="", note="legacy", context={})
    lci_telemetry._record_feedback(repo, rating="other", event_id=None, query="", artifact_id="", note="current", context={})

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    raw_ratings = {row["rating"]: row["count"] for row in report["feedback_by_rating"]}
    bucketed_ratings = {row["rating"]: row["count"] for row in report["feedback_rating_buckets"]}
    assert raw_ratings["great"] == 1
    assert bucketed_ratings["other"] == 2
    assert len(report["unknown_feedback_ratings"]) == 1
    assert report["unknown_feedback_ratings"][0]["rating"] == "great"
    assert report["unknown_feedback_ratings"][0]["count"] == 1


def test_usage_report_suppresses_negative_feedback_after_later_useful_feedback(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    now = datetime.now(timezone.utc)

    def ts(delta: timedelta) -> str:
        return (now + delta).isoformat(timespec="seconds").replace("+00:00", "Z")

    stamps = iter(
        [
            ts(timedelta(days=-4)),
            ts(timedelta(days=-4, seconds=1)),
            ts(timedelta(days=-1)),
            ts(timedelta(days=-1, seconds=1)),
            ts(timedelta()),
        ]
    )
    monkeypatch.setattr(lci_telemetry, "_utc_now", lambda: next(stamps))

    old_event = lci_telemetry._record_usage(
        repo,
        tool="knowledge_search",
        success=True,
        query="stale feedback query",
        result_count=2,
    )
    negative_feedback_id, _ = lci_telemetry._record_feedback(
        repo,
        rating="noisy",
        event_id=old_event,
        query="",
        artifact_id="",
        note="old ranking was noisy",
        context={},
    )
    # The timestamp-only suppression rule is retained solely for migrated rows.
    with sqlite3.connect(state_dir / "usage.sqlite") as conn:
        conn.execute(
            "UPDATE feedback SET linkage_status='legacy' WHERE id=?",
            (negative_feedback_id,),
        )
    useful_event = lci_telemetry._record_usage(
        repo,
        tool="knowledge_search",
        success=True,
        query="stale feedback query",
        result_count=2,
    )
    lci_telemetry._record_feedback(
        repo,
        rating="useful",
        event_id=useful_event,
        query="",
        artifact_id="",
        note="later check was useful",
        context={},
    )

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    assert report["live_recent_negative_feedback"][0]["effective_query"] == "stale feedback query"
    assert report["resolved_negative_feedback"][0]["effective_query"] == "stale feedback query"
    assert report["unresolved_negative_feedback"] == []
    assert all(item["type"] != "feedback_noisy" for item in report["improvement_candidates"])


def test_usage_report_recent_builds_exclude_failed_build_attempts(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    search = json.loads(plugin._handle_search({"query": "paperless review automation", "rebuild": True}))
    assert search["success"] is True
    lci_telemetry._record_usage(
        repo,
        tool="cli_build",
        client="cli",
        success=False,
        rebuilt=False,
        error="simulated failed build",
        db_path=state_dir / "index.sqlite",
        usage_db_path=state_dir / "usage.sqlite",
        index_metadata={
            "plugin_version": hermes_local_knowledge.__version__,
            "source_root_source": "config",
            "artifact_count": 999,
            "artifact_counts_by_type": {"skill": 999},
            "edge_count": 999,
            "build_duration_ms": 12,
        },
    )

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    assert report["recent_builds"]
    assert all(row["rebuilt"] == 1 for row in report["recent_builds"])
    assert all(row["index_artifact_count"] != 999 for row in report["recent_builds"])


def test_usage_report_persists_index_metadata_errors(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    event_id = lci_telemetry._record_usage(
        repo,
        tool="knowledge_search",
        success=True,
        query="corrupt index probe",
        result_count=0,
        db_path=state_dir / "index.sqlite",
        index_metadata={
            "plugin_version": hermes_local_knowledge.__version__,
            "source_root_source": "env",
            "state_dir_source": "env",
            "index_exists": True,
            "index_mtime": "2026-01-01T00:00:00Z",
            "index_metadata_error": "sqlite stats failed: DatabaseError: malformed database",
        },
    )
    assert isinstance(event_id, int)

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    assert report["latest_index_metadata"]["index_exists"] == 1
    assert "malformed database" in report["latest_index_metadata"]["index_metadata_error"]


def test_usage_report_masks_obvious_credentials_only_at_model_boundary(monkeypatch):
    raw_report = {
        "success": True,
        "total_events": 1,
        "improvement_candidates": [
            {
                "id": 73,
                "query": (
                    "open https://alex:hunter2@example.test/run "
                    "with api_key=sk-local api_key=\"quoted-local\" "
                    "password='single-local' {\"api_key\":\"json-local\"} "
                    "Authorization: Bearer bare-auth "
                    "Authorization: Bearer \"quoted-auth\" "
                    "Authorization: Bearer 'single-auth' "
                    '{"Authorization":"Bearer json-auth"}'
                ),
                "artifact_id": "skill:keep-stable-identity",
            }
        ],
    }

    class FakeService:
        usage_db_path = Path("/tmp/usage.sqlite")

        def usage_report(self, *, days: int, limit: int) -> dict[str, object]:
            assert (days, limit) == (30, 10)
            return raw_report

        def record_usage(self, **kwargs: object) -> int:
            return 91

    monkeypatch.setattr(plugin, "_service", lambda: FakeService())

    serialized = plugin._handle_usage_report({"days": 30, "limit": 10})
    payload = json.loads(serialized)

    for private_marker in (
        "hunter2",
        "sk-local",
        "quoted-local",
        "single-local",
        "json-local",
        "bare-auth",
        "quoted-auth",
        "single-auth",
        "json-auth",
    ):
        assert private_marker not in serialized
    assert "https://" + "<" + "redacted" + ">@example.test/run" in serialized
    assert payload["improvement_candidates"][0]["id"] == 73
    assert payload["improvement_candidates"][0]["artifact_id"] == "skill:keep-stable-identity"
    assert "hunter2" in raw_report["improvement_candidates"][0]["query"]
