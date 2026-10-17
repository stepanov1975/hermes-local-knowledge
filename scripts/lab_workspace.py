"""Bounded host reads of quiescent, untrusted candidate source trees."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_FILES = 10000
CHUNK_BYTES = 64 * 1024
CACHES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def inventory(root: Path, *, ignore_caches: bool = False) -> dict[str, Path]:
    """Validate the entire logical size budget before opening any source file."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Missing regular workspace")
    result: dict[str, Path] = {}
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("Workspace directory symlink")
        if ignore_caches:
            dirs[:] = [name for name in dirs if name not in CACHES]
        for name in files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("Workspace file symlink")
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Workspace must contain only unlinked regular files")
            total += info.st_size
            if info.st_size > MAX_FILE_BYTES or total > MAX_TREE_BYTES or len(result) >= MAX_FILES:
                raise ValueError("Candidate source size allowance exceeded")
            result[path.relative_to(root).as_posix()] = path
    return result


def chunks(path: Path):
    # Workers must already be quiescent. Still cap reads if a file grows after
    # inventory, and refuse links/nonregular replacements at open time.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
            raise ValueError("Candidate source type/size changed")
        count = 0
        while data := stream.read(CHUNK_BYTES):
            count += len(data)
            if count > MAX_FILE_BYTES or count > info.st_size:
                raise ValueError("Candidate source size changed")
            yield data
        if count != info.st_size:
            raise ValueError("Candidate source size changed")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for data in chunks(path):
        digest.update(data)
    return digest.hexdigest()


def read_files(root: Path) -> dict[str, bytes]:
    return {name: b"".join(chunks(path)) for name, path in inventory(root).items()}
