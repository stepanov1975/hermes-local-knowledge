"""Compatibility facade for runtime configuration and managed index lifecycle."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .artifacts import Artifact, Edge
from .config import Config, IndexSettings, OKFSettings, resolve_config
from .scanners import load_yaml_if_available
from .schemas import CONFIG_SECTION
from .service import LocalKnowledgeService

RuntimeConfig = Config
OKFConfig = OKFSettings
BuildIndexFn = Callable[..., tuple[list[Artifact], list[Edge]] | None]


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


def _get_hermes_home(override: Path | str | None = None) -> Path:
    if override not in (None, ""):
        return Path(str(override)).expanduser()
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _load_hermes_config(hermes_home: Path | str | None = None) -> dict[str, Any]:
    """Retain the legacy raw-config helper for compatibility callers."""

    if hermes_home not in (None, ""):
        config = load_yaml_if_available(Path(str(hermes_home)).expanduser() / "config.yaml")
        return config if isinstance(config, dict) else {}
    try:
        from hermes_cli.config import load_config  # type: ignore[import-not-found]

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


def _runtime_config(hermes_home: Path | str | None = None) -> RuntimeConfig:
    """Resolve configuration once through the format-4 configuration owner."""

    return resolve_config(hermes_home)


def _repo_root() -> Path:
    return _runtime_config().source_root


def _index_module(root: Path):
    """Return the late-bound compatibility module used by plugin monkeypatch seams."""

    from . import indexer

    return indexer


def _output_dir(root: Path) -> Path:
    return _runtime_config().state_dir


def _db_path(root: Path) -> Path:
    return _output_dir(root) / "index.sqlite"


def _usage_db_path(root: Path) -> Path:
    return _output_dir(root) / "usage.sqlite"


def _accepts_force(build_index_fn: BuildIndexFn) -> bool:
    try:
        parameters = inspect.signature(build_index_fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "force" or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _adapt_build_index(build_index_fn: BuildIndexFn) -> BuildIndexFn:
    """Adapt legacy four-argument test/caller builders without lifecycle ownership."""

    accepts_force = _accepts_force(build_index_fn)

    def adapted(
        root: Path,
        output_dir: Path,
        hermes_home: Path,
        settings: IndexSettings,
        *,
        force: bool,
    ) -> tuple[list[Artifact], list[Edge]] | None:
        if accepts_force:
            return build_index_fn(
                root,
                output_dir,
                hermes_home,
                settings,
                force=force,
            )
        return build_index_fn(root, output_dir, hermes_home, settings)

    return adapted


def _ensure_index(
    root: Path,
    *,
    rebuild: bool = False,
    build_index_fn: BuildIndexFn | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Delegate managed lifecycle to one service over one resolved config."""

    del root  # The resolved Config is the sole source of managed paths.
    config = _runtime_config()
    service = LocalKnowledgeService(
        config,
        build_index_fn=_adapt_build_index(build_index_fn) if build_index_fn is not None else None,
    )
    if rebuild:
        _artifacts, _edges, metadata = service.rebuild()
        return service.db_path, metadata
    return service.ensure_index()


def check_knowledge_available() -> bool:
    try:
        config = _runtime_config()
        return config.source_root.exists() and config.hermes_home.exists()
    except Exception:
        return False
