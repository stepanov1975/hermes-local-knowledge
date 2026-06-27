"""Runtime configuration and index lifecycle for the Hermes plugin."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import IndexSettings
from .scanners import load_yaml_if_available
from .schemas import CONFIG_SECTION, ROOT_ENV, STATE_ENV
from .storage import build_index

BuildIndexFn = Callable[[Path, Path, Path, IndexSettings], tuple[list[Any], list[Any]]]


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
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
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class RuntimeConfig:
    source_root: Path
    hermes_home: Path
    state_dir: Path
    index_settings: IndexSettings
    source_root_source: str = "default"
    state_dir_source: str = "default"
    include_markdown_docs_source: str = "default"
    warnings: tuple[str, ...] = ()


def _get_hermes_home(override: Path | str | None = None) -> Path:
    if override not in (None, ""):
        return Path(str(override)).expanduser()
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _load_hermes_config(hermes_home: Path | str | None = None) -> dict[str, Any]:
    if hermes_home not in (None, ""):
        config = load_yaml_if_available(Path(str(hermes_home)).expanduser() / "config.yaml")
        return config if isinstance(config, dict) else {}
    try:
        from hermes_cli.config import load_config  # type: ignore

        config = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:
        config = load_yaml_if_available(_get_hermes_home() / "config.yaml")
        return config if isinstance(config, dict) else {}


def _section_config(hermes_home: Path | str | None = None) -> dict[str, Any]:
    section = _load_hermes_config(hermes_home).get(CONFIG_SECTION, {})
    return section if isinstance(section, dict) else {}


def _first_config_value(section: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in section and section[key] not in (None, ""):
            return section[key]
    return default


def _config_value(*keys: str, default: Any = None) -> Any:
    return _first_config_value(_section_config(), *keys, default=default)


def _path_value(value: Any, default: Path) -> Path:
    if value in (None, ""):
        return default.expanduser()
    return Path(str(value)).expanduser()


def _tuple_value(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value in (None, ""):
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


def _runtime_warnings(source_root_source: str, source_root: Path, hermes_home: Path) -> tuple[str, ...]:
    warnings: list[str] = []
    if source_root_source == "default" and (hermes_home / "hermes-agent").exists():
        warnings.append(
            "local_knowledge.source_root is unset; defaulting to HERMES_HOME "
            f"({hermes_home}). Because HERMES_HOME/hermes-agent exists, indexing "
            "may be noisy. Prefer setting local_knowledge.source_root to a "
            "high-signal docs/customizations repo; runtime skills, cron jobs, "
            "and MCP config are still indexed from HERMES_HOME."
        )
    return tuple(warnings)


def _runtime_config(hermes_home: Path | str | None = None) -> RuntimeConfig:
    base_hermes_home = _get_hermes_home(hermes_home).resolve()
    section = _section_config(base_hermes_home if hermes_home not in (None, "") else None)
    configured_hermes_home = _first_config_value(section, "hermes_home")
    resolved_hermes_home = (
        base_hermes_home
        if hermes_home not in (None, "")
        else _path_value(configured_hermes_home, base_hermes_home).resolve()
    )

    env_root = os.environ.get(ROOT_ENV)
    config_root = _first_config_value(section, "source_root", "root")
    if env_root not in (None, ""):
        configured_root = env_root
        source_root_source = "env"
    elif config_root not in (None, ""):
        configured_root = config_root
        source_root_source = "config"
    else:
        configured_root = None
        source_root_source = "default"

    source_root = _path_value(
        configured_root,
        resolved_hermes_home,
    ).resolve()

    env_state_dir = os.environ.get(STATE_ENV)
    config_state_dir = _first_config_value(section, "state_dir", "index_dir")
    if env_state_dir not in (None, ""):
        configured_state_dir = env_state_dir
        state_dir_source = "env"
    elif config_state_dir not in (None, ""):
        configured_state_dir = config_state_dir
        state_dir_source = "config"
    else:
        configured_state_dir = None
        state_dir_source = "default"

    state_dir = _path_value(
        configured_state_dir,
        resolved_hermes_home / "local_knowledge",
    ).resolve()

    defaults = IndexSettings()
    known_entities = _tuple_value(
        _first_config_value(section, "known_entities", "entities"),
        defaults.known_entities,
    )
    include_markdown_docs_value = _first_config_value(section, "include_markdown_docs")
    include_markdown_docs_source = "config" if include_markdown_docs_value not in (None, "") else "default"
    settings = IndexSettings(
        custom_skill_dirs=_tuple_value(
            _first_config_value(section, "custom_skill_dirs"),
            defaults.custom_skill_dirs,
        ),
        script_dirs=_tuple_value(_first_config_value(section, "script_dirs"), defaults.script_dirs),
        memory_dirs=_tuple_value(_first_config_value(section, "memory_dirs"), defaults.memory_dirs),
        runbook_dirs=_tuple_value(_first_config_value(section, "runbook_dirs"), defaults.runbook_dirs),
        known_entities=known_entities,
        include_markdown_docs=_coerce_bool(
            include_markdown_docs_value,
            default=configured_root not in (None, ""),
        ),
    )
    return RuntimeConfig(
        source_root,
        resolved_hermes_home,
        state_dir,
        settings,
        source_root_source=source_root_source,
        state_dir_source=state_dir_source,
        include_markdown_docs_source=include_markdown_docs_source,
        warnings=_runtime_warnings(source_root_source, source_root, resolved_hermes_home),
    )


def _repo_root() -> Path:
    return _runtime_config().source_root


def _index_module(root: Path):
    from . import indexer

    return indexer


def _output_dir(root: Path) -> Path:
    return _runtime_config().state_dir


def _db_path(root: Path) -> Path:
    return _output_dir(root) / "index.sqlite"


def _usage_db_path(root: Path) -> Path:
    return _output_dir(root) / "usage.sqlite"


def _ensure_index(
    root: Path,
    *,
    rebuild: bool = False,
    build_index_fn: BuildIndexFn | None = None,
) -> tuple[Path, dict[str, Any]]:
    cfg = _runtime_config()
    db_path = cfg.state_dir / "index.sqlite"
    metadata: dict[str, Any] = {
        "root": str(cfg.source_root),
        "source_root_source": cfg.source_root_source,
        "state_dir": str(cfg.state_dir),
        "state_dir_source": cfg.state_dir_source,
        "include_markdown_docs_source": cfg.include_markdown_docs_source,
        "db_path": str(db_path),
        "warnings": list(cfg.warnings),
        "rebuilt": False,
    }
    if rebuild or not db_path.exists():
        build = build_index_fn or build_index
        artifacts, edges = build(
            cfg.source_root,
            cfg.state_dir,
            cfg.hermes_home,
            cfg.index_settings,
        )
        metadata.update(
            {
                "rebuilt": True,
                "artifact_count": len(artifacts),
                "edge_count": len(edges),
            }
        )
    return db_path, metadata


def check_knowledge_available() -> bool:
    try:
        cfg = _runtime_config()
        return cfg.source_root.exists() and cfg.hermes_home.exists()
    except Exception:
        return False
