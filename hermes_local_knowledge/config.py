"""Configuration models and the single resolver for local knowledge."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Config", "IndexSettings", "OKFSettings", "resolve_config"]

_CONFIG_SECTION = "local_knowledge"
_ROOT_ENV = "LOCAL_KNOWLEDGE_ROOT"
_STATE_ENV = "LOCAL_KNOWLEDGE_STATE_DIR"
_HERMES_HOME_ENV = "HERMES_HOME"
_DEFAULT_KNOWN_ENTITIES = ("Hermes", "GitHub", "MCP", "Cron")


@dataclass(frozen=True)
class IndexSettings:
    """Configurable source layout and scanner hints."""

    custom_skill_dirs: tuple[str, ...] = ("custom_skills",)
    script_dirs: tuple[str, ...] = ("scripts", "hermes_home/scripts")
    memory_dirs: tuple[str, ...] = ("memory",)
    runbook_dirs: tuple[str, ...] = ("docs",)
    known_entities: tuple[str, ...] = _DEFAULT_KNOWN_ENTITIES
    include_markdown_docs: bool = True
    exclude_dir_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class OKFSettings:
    """Settings for opportunistic tool-OKF generation."""

    enabled: bool = True
    auto_generate: bool = False
    max_candidates_per_session: int = 2
    max_generation_seconds: int = 120
    min_use_count: int = 1

    @property
    def max_worker_seconds(self) -> int:
        """Return the legacy name for the generation timeout."""

        return self.max_generation_seconds


@dataclass(frozen=True)
class Config:
    """Fully resolved local-knowledge configuration."""

    source_root: Path
    hermes_home: Path
    state_dir: Path
    index_settings: IndexSettings
    okf: OKFSettings = field(default_factory=OKFSettings)
    source_root_source: str = "default"
    state_dir_source: str = "default"
    include_markdown_docs_source: str = "default"
    warnings: tuple[str, ...] = ()
    router_skill_path: Path | None = None
    router_skill_path_source: str = "default"


def _present(value: Any) -> bool:
    return value not in (None, "")


def _first_value(section: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = section.get(key)
        if _present(value):
            return value
    return default


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not _present(value):
        return default
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = tuple(
            item.strip().strip("'\"")
            for item in text.split(",")
            if item.strip().strip("'\"")
        )
        return items or default
    if isinstance(value, (list, tuple)):
        items = tuple(str(item).strip() for item in value if str(item).strip())
        return items or default
    return default


def _resolve_path(value: Any, default: Path) -> Path:
    selected = default if not _present(value) else Path(str(value))
    return selected.expanduser().resolve()


def _resolve_profile_path(value: Any, *, hermes_home: Path, default: Path) -> Path:
    """Resolve a profile path without dereferencing its final symlink."""

    selected = default if not _present(value) else Path(str(value)).expanduser()
    if not selected.is_absolute():
        selected = hermes_home / selected
    return Path(os.path.abspath(selected))


def _strip_yaml_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif char in {"'", '"'}:
            quote = char if quote is None else None if quote == char else quote
        elif char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _parse_yaml_scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return None
    if text.startswith('"') and text.endswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    lowered = text.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", text):
        return float(text)
    return text


def _mapping_entry(value: str) -> tuple[str, str]:
    key, separator, raw_value = value.partition(":")
    if not separator:
        raise ValueError("mapping entry has no colon")
    return key.strip().strip("'\""), raw_value.strip()


def _parse_local_section(text: str) -> dict[str, Any]:
    """Parse the small YAML subset used by ``local_knowledge`` settings."""

    raw_lines = text.splitlines()
    section_start: int | None = None
    for index, raw_line in enumerate(raw_lines):
        if not raw_line.strip() or raw_line.lstrip().startswith("#") or raw_line[:1].isspace():
            continue
        try:
            key, inline_value = _mapping_entry(_strip_yaml_comment(raw_line))
        except ValueError:
            continue
        if key != _CONFIG_SECTION:
            continue
        if inline_value:
            return {}
        section_start = index + 1
        break
    if section_start is None:
        return {}

    section: dict[str, Any] = {}
    section_indent: int | None = None
    pending_key: str | None = None
    for raw_line in raw_lines[section_start:]:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0:
            break
        content = _strip_yaml_comment(raw_line.strip())
        if not content:
            continue
        if section_indent is None:
            section_indent = indent
        if indent == section_indent:
            key, raw_value = _mapping_entry(content)
            if raw_value:
                section[key] = _parse_yaml_scalar(raw_value)
                pending_key = None
            else:
                section[key] = None
                pending_key = key
            continue
        if section_indent is None or indent < section_indent or pending_key is None:
            raise ValueError("unexpected indentation in local_knowledge config")
        if content == "-" or content.startswith("- "):
            values = section.get(pending_key)
            if values is None:
                values = []
                section[pending_key] = values
            if not isinstance(values, list):
                raise ValueError("mixed YAML collection in local_knowledge config")
            values.append(_parse_yaml_scalar(content[1:].strip()))
            continue
        child_key, child_value = _mapping_entry(content)
        nested = section.get(pending_key)
        if nested is None:
            nested = {}
            section[pending_key] = nested
        if not isinstance(nested, dict):
            raise ValueError("mixed YAML collection in local_knowledge config")
        nested[child_key] = _parse_yaml_scalar(child_value)
    return section


def _section_from_file(hermes_home: Path) -> dict[str, Any]:
    try:
        text = (hermes_home / "config.yaml").read_text(encoding="utf-8")
        return _parse_local_section(text)
    except (OSError, UnicodeError, ValueError):
        return {}


def _load_section(hermes_home: Path, *, explicit_home: bool) -> dict[str, Any]:
    if not explicit_home:
        try:
            from hermes_cli.config import load_config  # type: ignore[import-not-found,import-untyped]

            loaded = load_config()
        except Exception:
            pass
        else:
            if isinstance(loaded, Mapping):
                section = loaded.get(_CONFIG_SECTION, {})
                return dict(section) if isinstance(section, Mapping) else {}
    return _section_from_file(hermes_home)


def _base_hermes_home(override: Path | str | None) -> Path:
    if _present(override):
        return Path(str(override)).expanduser().resolve()
    env_home = os.environ.get(_HERMES_HOME_ENV)
    if _present(env_home):
        return Path(str(env_home)).expanduser().resolve()
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found,import-untyped]

        return Path(get_hermes_home()).expanduser().resolve()
    except Exception:
        return (Path.home() / ".hermes").resolve()


def _resolve_okf_settings(section: Mapping[str, Any]) -> OKFSettings:
    defaults = OKFSettings()
    nested = section.get("okf", {})
    okf_section = nested if isinstance(nested, Mapping) else {}

    def value(name: str, *, default: Any) -> Any:
        return _first_value(
            okf_section,
            name,
            default=_first_value(section, f"okf_{name}", default=default),
        )

    max_generation_seconds = value(
        "max_generation_seconds",
        default=value("max_worker_seconds", default=defaults.max_generation_seconds),
    )
    return OKFSettings(
        enabled=_coerce_bool(value("enabled", default=defaults.enabled), default=defaults.enabled),
        auto_generate=_coerce_bool(
            value("auto_generate", default=defaults.auto_generate),
            default=defaults.auto_generate,
        ),
        max_candidates_per_session=_coerce_int(
            value("max_candidates_per_session", default=defaults.max_candidates_per_session),
            default=defaults.max_candidates_per_session,
            minimum=1,
            maximum=10,
        ),
        max_generation_seconds=_coerce_int(
            max_generation_seconds,
            default=defaults.max_generation_seconds,
            minimum=10,
            maximum=3600,
        ),
        min_use_count=_coerce_int(
            value("min_use_count", default=defaults.min_use_count),
            default=defaults.min_use_count,
            minimum=1,
            maximum=1000,
        ),
    )


def _warnings(source_root_source: str, hermes_home: Path) -> tuple[str, ...]:
    if source_root_source != "default" or not (hermes_home / "hermes-agent").exists():
        return ()
    return (
        "local_knowledge.source_root is unset; defaulting to HERMES_HOME "
        f"({hermes_home}). Because HERMES_HOME/hermes-agent exists, indexing "
        "may be noisy. Prefer setting local_knowledge.source_root to a "
        "high-signal docs/customizations repo; runtime skills, cron jobs, "
        "and MCP config are still indexed from HERMES_HOME.",
    )


def resolve_config(hermes_home: Path | str | None = None) -> Config:
    """Resolve local-knowledge settings from one Hermes profile and the environment."""

    explicit_home = _present(hermes_home)
    base_hermes_home = _base_hermes_home(hermes_home)
    section = _load_section(base_hermes_home, explicit_home=explicit_home)

    configured_hermes_home = _first_value(section, "hermes_home")
    resolved_hermes_home = (
        base_hermes_home
        if explicit_home
        else _resolve_path(configured_hermes_home, base_hermes_home)
    )

    env_root = os.environ.get(_ROOT_ENV)
    config_root = _first_value(section, "source_root", "root")
    if _present(env_root):
        configured_root = env_root
        source_root_source = "env"
    elif _present(config_root):
        configured_root = config_root
        source_root_source = "config"
    else:
        configured_root = None
        source_root_source = "default"
    source_root = _resolve_path(configured_root, resolved_hermes_home)

    env_state_dir = os.environ.get(_STATE_ENV)
    config_state_dir = _first_value(section, "state_dir", "index_dir")
    if _present(env_state_dir):
        configured_state_dir = env_state_dir
        state_dir_source = "env"
    elif _present(config_state_dir):
        configured_state_dir = config_state_dir
        state_dir_source = "config"
    else:
        configured_state_dir = None
        state_dir_source = "default"
    state_dir = _resolve_path(configured_state_dir, resolved_hermes_home / "local_knowledge")

    configured_router_skill_path = _first_value(section, "router_skill_path")
    router_skill_path_source = "config" if _present(configured_router_skill_path) else "default"
    router_skill_path = (
        _resolve_profile_path(
            configured_router_skill_path,
            hermes_home=resolved_hermes_home,
            default=resolved_hermes_home / "skills" / "local-knowledge-router" / "SKILL.md",
        )
        if router_skill_path_source == "config"
        else None
    )
    defaults = IndexSettings()
    include_markdown_docs_value = _first_value(section, "include_markdown_docs")
    include_markdown_docs_source = (
        "config" if _present(include_markdown_docs_value) else "default"
    )
    index_settings = IndexSettings(
        custom_skill_dirs=_coerce_tuple(
            _first_value(section, "custom_skill_dirs"),
            defaults.custom_skill_dirs,
        ),
        script_dirs=_coerce_tuple(
            _first_value(section, "script_dirs"),
            defaults.script_dirs,
        ),
        memory_dirs=_coerce_tuple(
            _first_value(section, "memory_dirs"),
            defaults.memory_dirs,
        ),
        runbook_dirs=_coerce_tuple(
            _first_value(section, "runbook_dirs"),
            defaults.runbook_dirs,
        ),
        known_entities=_coerce_tuple(
            _first_value(section, "known_entities", "entities"),
            defaults.known_entities,
        ),
        include_markdown_docs=_coerce_bool(
            include_markdown_docs_value,
            default=_present(configured_root),
        ),
        exclude_dir_names=_coerce_tuple(
            _first_value(section, "exclude_dir_names"),
            defaults.exclude_dir_names,
        ),
    )

    return Config(
        source_root=source_root,
        hermes_home=resolved_hermes_home,
        state_dir=state_dir,
        index_settings=index_settings,
        router_skill_path=router_skill_path,
        router_skill_path_source=router_skill_path_source,
        okf=_resolve_okf_settings(section),
        source_root_source=source_root_source,
        state_dir_source=state_dir_source,
        include_markdown_docs_source=include_markdown_docs_source,
        warnings=_warnings(source_root_source, resolved_hermes_home),
    )
