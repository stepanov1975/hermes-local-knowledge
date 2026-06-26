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

def _get_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()

def _load_hermes_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config  # type: ignore

        config = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:
        config = load_yaml_if_available(_get_hermes_home() / "config.yaml")
        return config if isinstance(config, dict) else {}

def _section_config() -> dict[str, Any]:
    section = _load_hermes_config().get(CONFIG_SECTION, {})
    return section if isinstance(section, dict) else {}

def _config_value(*keys: str, default: Any = None) -> Any:
    section = _section_config()
    for key in keys:
        if key in section and section[key] not in (None, ""):
            return section[key]
    return default

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

def _runtime_config() -> RuntimeConfig:
    hermes_home = _path_value(_config_value("hermes_home"), _get_hermes_home()).resolve()
    env_root = os.environ.get(ROOT_ENV)
    configured_root = env_root or _config_value("source_root", "root")
    source_root = _path_value(
        configured_root,
        hermes_home,
    ).resolve()
    state_dir = _path_value(
        os.environ.get(STATE_ENV) or _config_value("state_dir", "index_dir"),
        hermes_home / "local_knowledge",
    ).resolve()

    defaults = IndexSettings()
    known_entities = _tuple_value(
        _config_value("known_entities", "entities"),
        defaults.known_entities,
    )
    settings = IndexSettings(
        custom_skill_dirs=_tuple_value(
            _config_value("custom_skill_dirs"),
            defaults.custom_skill_dirs,
        ),
        script_dirs=_tuple_value(_config_value("script_dirs"), defaults.script_dirs),
        memory_dirs=_tuple_value(_config_value("memory_dirs"), defaults.memory_dirs),
        runbook_dirs=_tuple_value(_config_value("runbook_dirs"), defaults.runbook_dirs),
        known_entities=known_entities,
        include_markdown_docs=_coerce_bool(
            _config_value("include_markdown_docs"),
            default=configured_root not in (None, ""),
        ),
    )
    return RuntimeConfig(source_root, hermes_home, state_dir, settings)

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
        "state_dir": str(cfg.state_dir),
        "db_path": str(db_path),
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
