"""Runtime configuration and index lifecycle for the Hermes plugin."""
from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .models import IndexSettings
from .okf import index_dirty_tokens
from .scanners import load_yaml_if_available
from .schemas import CONFIG_SECTION, ROOT_ENV, STATE_ENV
from .storage import artifact_type_counts, build_index, index_metadata

BuildIndexFn = Callable[[Path, Path, Path, IndexSettings], tuple[list[Any], list[Any]]]
INDEX_BUILD_LOCK_NAME = "index_build.lock"
INDEX_BUILD_LOCK_WAIT_SECONDS = 120.0


def _index_build_lock_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / INDEX_BUILD_LOCK_NAME


def _acquire_index_build_lock(state_dir: Path) -> tuple[Path, int]:
    lock_path = _index_build_lock_path(state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + INDEX_BUILD_LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(f"timed out waiting for index build lock: {lock_path}")
            time.sleep(0.05)
    try:
        payload = json.dumps({"pid": os.getpid(), "acquired_at": time.time()}).encode("utf-8")
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)
    except Exception:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise
    return lock_path, fd


def _release_index_build_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


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
class OKFConfig:
    enabled: bool = True
    auto_generate: bool = False
    max_candidates_per_session: int = 2
    max_generation_seconds: int = 120
    min_use_count: int = 1

    @property
    def max_worker_seconds(self) -> int:
        """Compatibility alias for pre-0.3.1 configuration readers."""

        return self.max_generation_seconds


@dataclass(frozen=True)
class RuntimeConfig:
    source_root: Path
    hermes_home: Path
    state_dir: Path
    index_settings: IndexSettings
    okf: OKFConfig = field(default_factory=OKFConfig)
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


def _okf_config(section: dict[str, Any]) -> OKFConfig:
    defaults = OKFConfig()
    nested = section.get("okf", {})
    okf_section = nested if isinstance(nested, dict) else {}

    def value(name: str, *, flat_name: str | None = None, default: Any = None) -> Any:
        flat = flat_name or f"okf_{name}"
        return _first_config_value(okf_section, name, default=_first_config_value(section, flat, default=default))

    return OKFConfig(
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
            value(
                "max_generation_seconds",
                default=value("max_worker_seconds", default=defaults.max_generation_seconds),
            ),
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
        exclude_dir_names=_tuple_value(
            _first_config_value(section, "exclude_dir_names"),
            defaults.exclude_dir_names,
        ),
    )
    return RuntimeConfig(
        source_root,
        resolved_hermes_home,
        state_dir,
        settings,
        okf=_okf_config(section),
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
    dirty_tokens = index_dirty_tokens(cfg.state_dir)
    metadata: dict[str, Any] = {
        "plugin_version": __version__,
        "root": str(cfg.source_root),
        "source_root_source": cfg.source_root_source,
        "state_dir": str(cfg.state_dir),
        "state_dir_source": cfg.state_dir_source,
        "include_markdown_docs_source": cfg.include_markdown_docs_source,
        "db_path": str(db_path),
        "warnings": list(cfg.warnings),
        "rebuilt": False,
    }
    if rebuild or not db_path.exists() or dirty_tokens:
        _lock_path, lock_fd = _acquire_index_build_lock(cfg.state_dir)
        try:
            dirty_tokens = index_dirty_tokens(cfg.state_dir)
            if rebuild or not db_path.exists() or dirty_tokens:
                build = build_index_fn or build_index
                build_started = time.perf_counter()
                artifacts, edges = build(
                    cfg.source_root,
                    cfg.state_dir,
                    cfg.hermes_home,
                    cfg.index_settings,
                )
                metadata.update(
                    {
                        "rebuilt": True,
                        "build_duration_ms": int((time.perf_counter() - build_started) * 1000),
                        "artifact_count": len(artifacts),
                        "artifact_counts_by_type": artifact_type_counts(artifacts),
                        "edge_count": len(edges),
                    }
                )
                for token in dirty_tokens:
                    token.unlink(missing_ok=True)
        finally:
            _release_index_build_lock(lock_fd)
    metadata.update(index_metadata(db_path))
    return db_path, metadata


def check_knowledge_available() -> bool:
    try:
        cfg = _runtime_config()
        return cfg.source_root.exists() and cfg.hermes_home.exists()
    except Exception:
        return False
