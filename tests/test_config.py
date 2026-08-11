from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hermes_local_knowledge import config as config_module
from hermes_local_knowledge.config import (
    Config,
    ImplicitFeedbackSettings,
    IndexSettings,
    OKFSettings,
    resolve_config,
)


def write_config(hermes_home: Path, body: str) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(body, encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)


def block_hermes_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)


def test_public_surface_models_and_implicit_defaults(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()

    resolved = resolve_config(hermes_home)

    assert config_module.__all__ == [
        "Config",
        "IndexSettings",
        "OKFSettings",
        "ImplicitFeedbackSettings",
        "resolve_config",
    ]
    assert not hasattr(config_module, "RuntimeConfig")
    assert not hasattr(config_module, "OKFConfig")
    assert is_dataclass(Config)
    assert [field.name for field in fields(Config)] == [
        "source_root",
        "hermes_home",
        "state_dir",
        "index_settings",
        "okf",
        "implicit_feedback",
        "source_root_source",
        "state_dir_source",
        "include_markdown_docs_source",
        "warnings",
        "router_skill_path",
        "router_skill_path_source",
    ]
    assert resolved == Config(
        source_root=hermes_home.resolve(),
        hermes_home=hermes_home.resolve(),
        state_dir=(hermes_home / "local_knowledge").resolve(),
        index_settings=IndexSettings(include_markdown_docs=False),
    )
    assert resolved.source_root_source == "default"
    assert resolved.state_dir_source == "default"
    assert resolved.include_markdown_docs_source == "default"
    assert resolved.warnings == ()
    assert resolved.router_skill_path is None
    assert resolved.router_skill_path_source == "default"
    assert resolved.implicit_feedback == ImplicitFeedbackSettings()
    assert resolved.implicit_feedback.enabled is False
    assert resolved.implicit_feedback.min_confirmations == 2
    assert resolved.implicit_feedback.max_generic_queries == 5
    assert IndexSettings() == IndexSettings(
        custom_skill_dirs=("custom_skills",),
        script_dirs=("scripts", "hermes_home/scripts"),
        memory_dirs=("memory",),
        runbook_dirs=("docs",),
        known_entities=("Hermes", "GitHub", "MCP", "Cron"),
        include_markdown_docs=True,
        exclude_dir_names=(),
    )
    assert resolved.okf == OKFSettings(
        enabled=True,
        auto_generate=False,
        max_candidates_per_session=2,
        max_generation_seconds=120,
        min_use_count=1,
    )
    assert resolved.okf.max_worker_seconds == 120
    with pytest.raises(FrozenInstanceError):
        resolved.state_dir = tmp_path / "other"  # type: ignore[misc]


def test_router_skill_path_resolves_from_hermes_home_without_dereferencing_symlink(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    external_skill = tmp_path / "customization-repo" / "SKILL.md"
    external_skill.parent.mkdir()
    external_skill.write_text("---\nname: local-knowledge-router\n---\n", encoding="utf-8")
    runtime_skill = hermes_home / "skills" / "note-taking" / "local-knowledge-router" / "SKILL.md"
    runtime_skill.parent.mkdir(parents=True)
    runtime_skill.symlink_to(external_skill)
    write_config(
        hermes_home,
        """local_knowledge:
  router_skill_path: skills/note-taking/local-knowledge-router/SKILL.md
""",
    )

    resolved = resolve_config(hermes_home)

    assert resolved.router_skill_path is not None
    assert resolved.router_skill_path == runtime_skill
    assert resolved.router_skill_path.resolve() == external_skill.resolve()
    assert resolved.router_skill_path_source == "config"


@pytest.mark.parametrize(
    ("config_body", "env_root", "expected", "expected_source"),
    [
        ("local_knowledge: {{}}\n", None, "hermes-home", "default"),
        ("local_knowledge:\n  source_root: {root}\n", None, "config-root", "config"),
        ("local_knowledge:\n  root: {root}\n", None, "config-root", "config"),
        ("local_knowledge: {{}}\n", "env-root", "env-root", "env"),
    ],
)
def test_root_provenance_controls_markdown_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_body: str,
    env_root: str | None,
    expected: str,
    expected_source: str,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    config_root = tmp_path / "config-root"
    rendered = config_body.format(root=config_root)
    write_config(hermes_home, rendered)
    if env_root is not None:
        monkeypatch.setenv("LOCAL_KNOWLEDGE_ROOT", str(tmp_path / env_root))

    resolved = resolve_config(hermes_home)

    assert resolved.source_root == (tmp_path / expected).resolve()
    assert resolved.source_root_source == expected_source
    assert resolved.index_settings.include_markdown_docs is (expected_source != "default")
    assert resolved.include_markdown_docs_source == "default"


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [("false", False), ("'off'", False), ("true", True), ("'yes'", True), ("'invalid'", True)],
)
def test_explicit_markdown_setting_overrides_root_default(
    tmp_path: Path,
    raw_value: str,
    expected: bool,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    source_root = tmp_path / "source"
    write_config(
        hermes_home,
        f"""local_knowledge:
  source_root: {source_root}
  include_markdown_docs: {raw_value}
""",
    )

    resolved = resolve_config(hermes_home)

    assert resolved.index_settings.include_markdown_docs is expected
    assert resolved.include_markdown_docs_source == "config"


def test_canonical_paths_beat_aliases_and_environment_beats_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    canonical_root = tmp_path / "canonical-root"
    alias_root = tmp_path / "alias-root"
    canonical_state = tmp_path / "canonical-state"
    alias_state = tmp_path / "alias-state"
    env_root = tmp_path / "env-root"
    env_state = tmp_path / "env-state"
    write_config(
        hermes_home,
        f"""local_knowledge:
  source_root: {canonical_root}
  root: {alias_root}
  state_dir: {canonical_state}
  index_dir: {alias_state}
  known_entities: [Canonical, Entity]
  entities: [Alias, Entity]
""",
    )

    configured = resolve_config(hermes_home)
    assert configured.source_root == canonical_root.resolve()
    assert configured.state_dir == canonical_state.resolve()
    assert configured.index_settings.known_entities == ("Canonical", "Entity")
    assert configured.source_root_source == "config"
    assert configured.state_dir_source == "config"

    monkeypatch.setenv("LOCAL_KNOWLEDGE_ROOT", str(env_root))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_STATE_DIR", str(env_state))
    overridden = resolve_config(hermes_home)
    assert overridden.source_root == env_root.resolve()
    assert overridden.state_dir == env_state.resolve()
    assert overridden.source_root_source == "env"
    assert overridden.state_dir_source == "env"


def test_aliases_and_scanner_sequence_coercion(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes-home"
    source_root = tmp_path / "source"
    state_dir = tmp_path / "state"
    write_config(
        hermes_home,
        f"""local_knowledge:
  root: {source_root}
  index_dir: {state_dir}
  custom_skill_dirs: ' alpha, beta '
  script_dirs: '[scripts_one, "scripts_two"]'
  memory_dirs: []
  runbook_dirs: 17
  entities:
    - Quartz
    - Acme
  exclude_dir_names: [build, dist]
""",
    )

    resolved = resolve_config(hermes_home)

    assert resolved.source_root == source_root.resolve()
    assert resolved.state_dir == state_dir.resolve()
    assert resolved.index_settings == IndexSettings(
        custom_skill_dirs=("alpha", "beta"),
        script_dirs=("scripts_one", "scripts_two"),
        memory_dirs=("memory",),
        runbook_dirs=("docs",),
        known_entities=("Quartz", "Acme"),
        include_markdown_docs=True,
        exclude_dir_names=("build", "dist"),
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            """  okf_enabled: false
  okf_auto_generate: on
  okf_max_candidates_per_session: 99
  okf_max_worker_seconds: 333
  okf_min_use_count: 0
""",
            OKFSettings(False, True, 10, 333, 1),
        ),
        (
            """  okf_enabled: false
  okf_auto_generate: false
  okf_max_candidates_per_session: 9
  okf_max_generation_seconds: 180
  okf_min_use_count: 7
  okf:
    enabled: true
    auto_generate: true
    max_candidates_per_session: 0
    max_generation_seconds: 0
    max_worker_seconds: 999
    min_use_count: 5000
""",
            OKFSettings(True, True, 1, 10, 1000),
        ),
        (
            """  okf_enabled: maybe
  okf_auto_generate: maybe
  okf_max_candidates_per_session: invalid
  okf_max_worker_seconds: invalid
  okf_min_use_count: invalid
""",
            OKFSettings(),
        ),
        (
            """  okf_max_generation_seconds: 222
  okf:
    max_worker_seconds: 444
""",
            OKFSettings(max_generation_seconds=222),
        ),
    ],
)
def test_okf_nested_flat_legacy_fallback_and_coercion(
    tmp_path: Path,
    body: str,
    expected: OKFSettings,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    write_config(hermes_home, f"local_knowledge:\n{body}")

    resolved = resolve_config(hermes_home)

    assert resolved.okf == expected
    assert resolved.okf.max_worker_seconds == expected.max_generation_seconds


def test_configured_hermes_home_and_explicit_override_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_home = tmp_path / "selected-home"
    configured_home = tmp_path / "configured-runtime-home"
    explicit_home = tmp_path / "explicit-home"
    configured_root = tmp_path / "configured-root"
    write_config(
        selected_home,
        f"""local_knowledge:
  hermes_home: {configured_home}
  source_root: {configured_root}
""",
    )
    write_config(
        explicit_home,
        """local_knowledge:
  hermes_home: /must/not/win
""",
    )
    monkeypatch.setenv("HERMES_HOME", str(selected_home))
    block_hermes_host(monkeypatch)

    configured = resolve_config()
    explicit = resolve_config(explicit_home)

    assert configured.hermes_home == configured_home.resolve()
    assert configured.source_root == configured_root.resolve()
    assert configured.state_dir == (configured_home / "local_knowledge").resolve()
    assert explicit.hermes_home == explicit_home.resolve()
    assert explicit.source_root == explicit_home.resolve()
    assert explicit.state_dir == (explicit_home / "local_knowledge").resolve()


def test_active_home_uses_host_loader_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    host_root = tmp_path / "host-root"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    host_calls: list[str] = []
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.__path__ = []  # type: ignore[attr-defined]
    host_config = ModuleType("hermes_cli.config")

    def load_config() -> dict[str, Any]:
        host_calls.append("load_config")
        return {"local_knowledge": {"source_root": str(host_root)}}

    host_config.load_config = load_config  # type: ignore[attr-defined]
    hermes_cli.config = host_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", host_config)

    resolved = resolve_config()

    assert host_calls == ["load_config"]
    assert resolved.hermes_home == hermes_home.resolve()
    assert resolved.source_root == host_root.resolve()


def test_explicit_hermes_home_reads_that_config_without_host_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient_home = tmp_path / "ambient-home"
    explicit_home = tmp_path / "explicit-home"
    ambient_root = tmp_path / "ambient-root"
    explicit_root = tmp_path / "explicit-root"
    write_config(ambient_home, f"local_knowledge:\n  source_root: {ambient_root}\n")
    write_config(explicit_home, f"local_knowledge:\n  source_root: {explicit_root}\n")
    monkeypatch.setenv("HERMES_HOME", str(ambient_home))

    host_calls: list[str] = []
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.__path__ = []  # type: ignore[attr-defined]
    host_config = ModuleType("hermes_cli.config")

    def load_config() -> dict[str, Any]:
        host_calls.append("load_config")
        return {"local_knowledge": {"source_root": str(ambient_root)}}

    host_config.load_config = load_config  # type: ignore[attr-defined]
    hermes_cli.config = host_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", host_config)

    resolved = resolve_config(explicit_home)

    assert host_calls == []
    assert resolved.hermes_home == explicit_home.resolve()
    assert resolved.source_root == explicit_root.resolve()


def test_default_hermes_home_needs_no_host_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block_hermes_host(monkeypatch)

    resolved = resolve_config()

    expected_home = (tmp_path / "home" / ".hermes").resolve()
    assert resolved.hermes_home == expected_home
    assert resolved.source_root == expected_home
    assert resolved.state_dir == expected_home / "local_knowledge"


def test_standard_library_fallback_has_no_hermes_or_yaml_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    source_root = tmp_path / "source"
    state_dir = tmp_path / "state"
    write_config(
        hermes_home,
        f"""unrelated:
  deeply:
    nested:
      - value
local_knowledge:
  source_root: {source_root} # selected root
  state_dir: {state_dir}
  known_entities:
    - Hermes
    - 'Acme Cloud'
  okf:
    enabled: false
    auto_generate: true
    max_candidates_per_session: 4
""",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    block_hermes_host(monkeypatch)

    resolved = resolve_config()

    assert resolved.hermes_home == hermes_home.resolve()
    assert resolved.source_root == source_root.resolve()
    assert resolved.state_dir == state_dir.resolve()
    assert resolved.index_settings.known_entities == ("Hermes", "Acme Cloud")
    assert resolved.okf == OKFSettings(
        enabled=False,
        auto_generate=True,
        max_candidates_per_session=4,
    )


@pytest.mark.parametrize("has_source_checkout", [False, True], ids=["absent", "present"])
def test_implicit_root_warning_is_conditional(
    tmp_path: Path,
    has_source_checkout: bool,
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    if has_source_checkout:
        (hermes_home / "hermes-agent").mkdir()

    implicit = resolve_config(hermes_home)

    if has_source_checkout:
        assert len(implicit.warnings) == 1
        assert "Because HERMES_HOME/hermes-agent exists" in implicit.warnings[0]
        assert str(hermes_home.resolve()) in implicit.warnings[0]
    else:
        assert implicit.warnings == ()

    configured_root = tmp_path / "configured-root"
    write_config(hermes_home, f"local_knowledge:\n  source_root: {configured_root}\n")
    assert resolve_config(hermes_home).warnings == ()


def test_implicit_feedback_settings_override(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    write_config(
        hermes_home,
        "local_knowledge:\n"
        "  implicit_feedback:\n"
        "    enabled: true\n"
        "    min_confirmations: 3\n"
        "    max_generic_queries: 8\n",
    )

    resolved = resolve_config(hermes_home)

    assert resolved.implicit_feedback.enabled is True
    assert resolved.implicit_feedback.min_confirmations == 3
    assert resolved.implicit_feedback.max_generic_queries == 8


def test_implicit_feedback_settings_flat_aliases(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    write_config(
        hermes_home,
        "local_knowledge:\n"
        "  implicit_feedback_enabled: true\n"
        "  implicit_feedback_min_confirmations: 4\n",
    )

    resolved = resolve_config(hermes_home)

    assert resolved.implicit_feedback.enabled is True
    assert resolved.implicit_feedback.min_confirmations == 4
    assert resolved.implicit_feedback.max_generic_queries == 5
