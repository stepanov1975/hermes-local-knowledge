"""Filesystem path helpers for safe local knowledge scanning."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence

from .constants import DEFAULT_ROOT, DEFAULT_STATE_DIR_NAME, EXCLUDED_DIR_NAMES


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

def should_skip_path(path: Path) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in path.parts)

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
) -> Iterable[Path]:
    if not root.exists():
        return
    allowed = tuple((allowed_roots or (root,)))
    resolved_allowed_roots = tuple(path.expanduser().resolve() for path in allowed if path.exists())
    seen_dirs: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        current_dir = Path(dirpath)
        if not is_within_allowed_roots(current_dir, resolved_allowed_roots):
            dirnames[:] = []
            continue
        current_key = stat_key(current_dir)
        if current_key is None or current_key in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(current_key)

        kept_dirnames: list[str] = []
        for dirname in sorted(dirnames):
            if dirname in EXCLUDED_DIR_NAMES:
                continue
            child = current_dir / dirname
            if not is_within_allowed_roots(child, resolved_allowed_roots):
                continue
            child_key = stat_key(child)
            if child_key is None or child_key in seen_dirs:
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for file_name in sorted(filenames):
            path = Path(dirpath) / file_name
            if should_skip_path(path):
                continue
            if not is_within_allowed_roots(path, resolved_allowed_roots):
                continue
            if filename is not None and file_name != filename:
                continue
            if suffixes is not None and path.suffix not in suffixes:
                continue
            yield path
