"""Read-only, bounded whole-Markdown evidence for private routing investigations."""
from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

from . import index
from .config import Config

MAX_SOURCE_BYTES = 24_000
MAX_TOTAL_BYTES = 96_000
MAX_SOURCES = 8
MAX_CANDIDATES = 32
MARKDOWN_TYPES = frozenset({"skill", "skill_support_doc", "runbook", "doc", "memory_doc"})


def _db(cfg: Config) -> Path:
    path = cfg.state_dir / "index.sqlite"
    if index.index_source_root(path) != str(cfg.source_root.resolve()):
        raise ValueError("index_root_mismatch")
    return path


def _source_path(cfg: Config, row: dict[str, Any]) -> Path:
    if row.get("type") not in MARKDOWN_TYPES:
        raise ValueError("unsupported_source")
    path = Path(str(row["path"])).expanduser()
    if not path.is_absolute():
        path = cfg.source_root / path
    if row["type"] == "skill":
        path = path / "SKILL.md"
    # Index membership is necessary, not permission to follow a newly retargeted symlink.
    roots = [cfg.source_root.resolve(), (cfg.hermes_home / "skills").resolve()]
    resolved = path.resolve(strict=True)
    excluded = {".archive", ".git", ".env", *cfg.index_settings.exclude_dir_names}
    if (resolved.suffix.lower() != ".md" or not resolved.is_file()
            or excluded.intersection(path.parts) or excluded.intersection(resolved.parts)
            or not any(resolved.is_relative_to(root) for root in roots)):
        raise ValueError("unregistered_source_path")
    return resolved


def _read_source_bytes_windows(path: Path) -> bytes:
    """Pin every component with Win32 handles before opening its children.

    OPEN_REPARSE_POINT prevents following the component being opened. Keeping
    all ancestors open without WRITE/DELETE sharing prevents their conversion
    to reparse points or replacement while a later full-path open is in flight.
    Attribute checks and the bounded read use the same final handle. Unlike a
    pathname recheck, this also covers junctions and ancestor-swap races.
    """
    import ctypes
    from ctypes import wintypes

    # Resolve Windows-only ctypes exports lazily so POSIX imports/type checks
    # do not require a Windows runtime.
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    win_error = getattr(ctypes, "WinError")
    last_error = getattr(ctypes, "get_last_error")
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    info.restype = wintypes.BOOL
    file_type = kernel.GetFileType
    file_type.argtypes = [wintypes.HANDLE]
    file_type.restype = wintypes.DWORD
    read = kernel.ReadFile
    read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    read.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    class AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    # Canonical drive/UNC paths only; extended syntax avoids legacy Win32 path
    # normalization and MAX_PATH truncation. _source_path has already resolved
    # intentional links, so any remaining reparse point is a raced replacement.
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unregistered_source_path")
    handles: list[Any] = []
    try:
        components = [*reversed(path.parents), path]
        for position, component in enumerate(components):
            name = str(component)
            if not name.startswith("\\\\?\\"):
                name = ("\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\")
                        else "\\\\?\\" + name)
            leaf = position == len(components) - 1
            # GENERIC_READ also on directories: attribute-only access does NOT
            # participate in Windows sharing checks and would not pin them.
            # FILE_SHARE_READ only; OPEN_EXISTING; BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
            handle = create(name, 0x80000000, 0x1, None,
                            3, 0x02000000 | 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise win_error(last_error())
            handles.append(handle)
            attributes = AttributeTagInfo()
            if not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
                raise win_error(last_error())
            if (attributes.attributes & 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
                    or bool(attributes.attributes & 0x10) == leaf
                    or file_type(handle) != 1):  # FILE_TYPE_DISK only
                raise ValueError("unregistered_source_path")
        buffer = ctypes.create_string_buffer(MAX_SOURCE_BYTES + 1)
        count = wintypes.DWORD()
        if not read(handles[-1], buffer, len(buffer), ctypes.byref(count), None):
            raise win_error(last_error())
        return buffer.raw[:count.value]
    finally:
        for handle in reversed(handles):
            close(handle)


def _read_source_bytes(path: Path) -> bytes:
    """Open a policy-approved canonical path without re-following any symlinks.

    Pin every ancestor starting at the filesystem root, including the approved
    root itself. A replaced entry is rejected; a directory already opened stays
    pinned even if its name is subsequently replaced. Existing symlink sources
    remain supported by _source_path's canonicalization before this walk.
    """
    if os.name == "nt":
        return _read_source_bytes_windows(path)
    if (os.name != "posix" or os.open not in os.supports_dir_fd
            or not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))):
        # A pathname reopen is not a safe fallback on unsupported platforms.
        raise OSError(errno.ENOTSUP, "secure_source_open_unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        # NONBLOCK prevents a raced-in FIFO from hanging before fstat rejects it.
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(fd)
        fd = child
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unregistered_source_path")
        # Read and validate the very same descriptor, never reopen its pathname.
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(MAX_SOURCE_BYTES + 1)
    finally:
        os.close(fd)


def read_source(cfg: Config, artifact_id: str) -> dict[str, Any]:
    """Re-resolve index identity and hash the complete current file, never a prefix."""
    row = index.get_artifact(_db(cfg), artifact_id)
    if row is None:
        raise ValueError("missing_source")
    path = _source_path(cfg, row)
    raw = _read_source_bytes(path)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("source_too_large")
    text = raw.decode("utf-8")
    if not text.strip():
        raise ValueError("empty_source")
    return {"id": artifact_id, "type": row["type"], "locator": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "text": text}


def identity(source: dict[str, Any]) -> dict[str, Any]:
    return {key: source[key] for key in ("id", "type", "locator", "sha256")}


def sources_current(cfg: Config, identities: list[dict[str, Any]]) -> bool:
    if not identities or len(identities) > MAX_SOURCES:
        return False
    try:
        return all(identity(read_source(cfg, item["id"])) == item for item in identities)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


class Evidence:
    """A single case's in-memory search/read allowance; content is never persisted."""

    def __init__(self, cfg: Config, artifact_type: str) -> None:
        self.cfg = cfg
        self.artifact_type = artifact_type
        self.candidates: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.searches = 0
        self.search_history: list[dict[str, Any]] = []
        self.bytes_read = 0
        self.refusals: dict[str, str] = {}

    def search(self, query: str) -> None:
        if self.searches >= 6 or not query.strip() or len(query) > 600:
            raise ValueError("search_budget")
        self.searches += 1
        before = set(self.candidates)
        rows = index.search_index(_db(self.cfg), query, limit=12,
                                  artifact_type=self.artifact_type or None)
        for row in rows:
            if row["type"] not in MARKDOWN_TYPES:
                continue
            if len(self.candidates) >= MAX_CANDIDATES:
                break
            self.candidates[row["id"]] = {key: str(row.get(key, ""))[:600]
                                          for key in ("id", "type", "title", "path", "summary")}
        self.search_history.append({"query": query,
                                    "new_candidate_ids": sorted(set(self.candidates) - before)})

    def include(self, ids: list[str]) -> None:
        """Resolve captured baseline/stored-route IDs, without another lexical search."""
        for artifact_id in ids:
            if artifact_id in self.candidates:
                continue
            if len(self.candidates) >= MAX_CANDIDATES:
                raise ValueError("candidate_budget")
            row = index.get_artifact(_db(self.cfg), artifact_id)
            if row is None:
                self.refusals[artifact_id] = "missing_source"
                continue
            if self.artifact_type and row.get("type") != self.artifact_type:
                self.refusals[artifact_id] = "artifact_type_mismatch"
                continue
            self.candidates[artifact_id] = {key: str(row.get(key, ""))[:600]
                                            for key in ("id", "type", "title", "path", "summary")}

    def read(self, artifact_id: str, *, refresh: bool = False) -> dict[str, Any]:
        if artifact_id not in self.candidates:
            raise ValueError("unsearched_source")
        if artifact_id in self.sources and not refresh:
            return self.sources[artifact_id]
        if artifact_id not in self.sources and len(self.sources) >= MAX_SOURCES:
            raise ValueError("source_count_budget")
        source = read_source(self.cfg, artifact_id)
        if source["type"] != self.candidates[artifact_id]["type"]:
            raise ValueError("source_identity_changed")
        # Refreshes replace an existing receipt rather than expanding the packet.
        previous = self.sources.get(artifact_id, {}).get("bytes", 0)
        new_size = self.bytes_read - previous + source["bytes"]
        if new_size > MAX_TOTAL_BYTES:
            raise ValueError("source_bytes_budget")
        self.sources[artifact_id] = source
        self.bytes_read = new_size
        return source

    def packet(self) -> dict[str, Any]:
        return {"search_history": self.search_history,
                "candidates": list(self.candidates.values()), "read_refusals": self.refusals, "sources": [
            {**identity(source), "lines": [f"{i}: {line}" for i, line in
             enumerate(source["text"].splitlines(), 1)]}
            for source in self.sources.values()]}

    def try_read(self, artifact_id: str) -> bool:
        try:
            self.read(artifact_id)
            return True
        except (OSError, UnicodeError):
            reason = "source_unavailable"
        except ValueError as exc:
            reason = str(exc)
            if reason not in {"source_too_large", "empty_source", "missing_source", "unsupported_source",
                              "unregistered_source_path", "source_count_budget", "source_bytes_budget"}:
                raise
        self.refusals[artifact_id] = reason
        return False


def checked_citations(value: Any, sources: dict[str, dict[str, Any]],
                      required_ids: list[str]) -> list[dict[str, Any]]:
    """Reject invented IDs, hashes, locators, and out-of-file line ranges."""
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise ValueError("invalid_citations")
    checked = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid_citation")
        source = sources.get(item.get("id", ""))
        if source is None or any(item.get(key) != source[key]
                                 for key in ("id", "locator", "sha256")):
            raise ValueError("unread_citation")
        start, end = item.get("start_line"), item.get("end_line")
        if (type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= len(source["text"].splitlines()) or end - start > 80):
            raise ValueError("invalid_citation_range")
        checked.append({key: item[key] for key in
                        ("id", "locator", "sha256", "start_line", "end_line")})
    if not set(required_ids).issubset({item["id"] for item in checked}):
        raise ValueError("uncited_route")
    return checked
