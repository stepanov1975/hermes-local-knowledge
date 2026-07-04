"""Filesystem path helpers for safe local knowledge scanning."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence

from .constants import DEFAULT_ROOT, DEFAULT_STATE_DIR_NAME, EXCLUDED_DIR_NAMES


def _effective_excluded_names(extra: Sequence[str] | None = None) -> set[str]:
    """Return the excluded directory name set, merging defaults with user-supplied extras."""
    if not extra:
        return set(EXCLUDED_DIR_NAMES)
    return EXCLUDED_DIR_NAMES | set(extra)


def repo_root() -> Path:
    return DEFAULT_ROOT

def hermes_home_from_env() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()

def default_output_dir(hermes_home: Path | None = None) -> Path:
    """Default generated state directory outside the indexed source tree."""
    base = hermes_home.expanduser() if hermes_home is not None else hermes_home_from_env()
    return base / DEFAULT_STATE_DIR_NAME

def display_path(path: Path, *, root: Path | None = None) -> str:
    expanded = path.expanduser()
    if root is not None:
        try:
            return expanded.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    try:
        return "~/" + expanded.resolve().relative_to(Path.home()).as_posix()
    except ValueError:
        return expanded.as_posix()

def should_skip_path(path: Path, excluded_dir_names: Sequence[str] | None = None) -> bool:
    excluded = _effective_excluded_names(excluded_dir_names)
    return any(part in excluded for part in path.parts)

def path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

def is_within_allowed_roots(path: Path, allowed_roots: Sequence[Path]) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return any(path_is_relative_to(resolved, allowed_root) for allowed_root in allowed_roots)

def has_excluded_part_within_allowed_roots(
    path: Path,
    allowed_roots: Sequence[Path],
    excluded_dir_names: Sequence[str] | None = None,
) -> bool:
    """Return whether a resolved path crosses an excluded name under an allowed root."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return True
    for allowed_root in allowed_roots:
        try:
            rel = resolved.relative_to(allowed_root)
        except ValueError:
            continue
        return should_skip_path(rel, excluded_dir_names)
    return True

def stat_key(path: Path) -> tuple[int, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_dev, stat_result.st_ino)

def iter_files_followlinks(
    root: Path,
    filename: str | None = None,
    suffixes: set[str] | None = None,
    *,
    allowed_roots: Sequence[Path] | None = None,
    followlinks: bool = True,
    excluded_dir_names: Sequence[str] | None = None,
) -> Iterable[Path]:
    if not root.exists():
        return
    allowed = tuple((allowed_roots or (root,)))
    resolved_allowed_roots = tuple(path.expanduser().resolve() for path in allowed if path.exists())
    excluded = _effective_excluded_names(excluded_dir_names)
    seen_dirs: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=followlinks):
        current_dir = Path(dirpath)
        if not is_within_allowed_roots(current_dir, resolved_allowed_roots):
            dirnames[:] = []
            continue
        if has_excluded_part_within_allowed_roots(current_dir, resolved_allowed_roots, excluded_dir_names):
            dirnames[:] = []
            continue
        current_key = stat_key(current_dir)
        if current_key is None or current_key in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(current_key)

        kept_dirnames: list[str] = []
        pending_dir_keys: set[tuple[int, int]] = set()
        for dirname in sorted(dirnames, key=lambda name: ((current_dir / name).is_symlink(), name)):
            if dirname in excluded:
                continue
            child = current_dir / dirname
            if not is_within_allowed_roots(child, resolved_allowed_roots):
                continue
            if has_excluded_part_within_allowed_roots(child, resolved_allowed_roots, excluded_dir_names):
                continue
            child_key = stat_key(child)
            if child_key is None or child_key in seen_dirs or child_key in pending_dir_keys:
                continue
            pending_dir_keys.add(child_key)
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for file_name in sorted(filenames):
            path = Path(dirpath) / file_name
            if not is_within_allowed_roots(path, resolved_allowed_roots):
                continue
            if has_excluded_part_within_allowed_roots(path, resolved_allowed_roots, excluded_dir_names):
                continue
            if filename is not None and file_name != filename:
                continue
            if suffixes is not None and path.suffix not in suffixes:
                continue
            yield path