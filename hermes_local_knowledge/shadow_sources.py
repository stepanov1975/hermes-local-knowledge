"""Read-only exact source evidence for private whole-artifact routing investigations."""
from __future__ import annotations

import errno
import hashlib
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any

from . import index
from .artifacts import _SCRIPT_SUFFIXES as SCRIPT_SUFFIXES
from .config import Config

MAX_FILE_BYTES = 1_000_000
MAX_EXCERPT_LINES = 160
MAX_SOURCE_BYTES = 24_000
MAX_TOTAL_BYTES = 96_000
MAX_SOURCES = 8
MAX_RANGES = 8
MAX_LOCATIONS = 32
MAX_CANDIDATES = 32
MARKDOWN_TYPES = frozenset({"skill", "skill_support_doc", "runbook", "doc", "memory_doc"})
SOURCE_TYPES = MARKDOWN_TYPES | {"script"}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:[a-z0-9_]*(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|authorization)[a-z0-9_]*)\b"
    r"[\"\']?\s*[:=]\s*\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|https?://[^/\s]+:[^/\s]+@"
)


def _db(cfg: Config) -> Path:
    path = cfg.state_dir / "index.sqlite"
    if index.index_source_root(path) != str(cfg.source_root.resolve()):
        raise ValueError("index_root_mismatch")
    return path


def _source_path(cfg: Config, row: dict[str, Any]) -> Path:
    if row.get("type") not in SOURCE_TYPES:
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
    suffixes = SCRIPT_SUFFIXES if row["type"] == "script" else {".md"}
    if resolved.suffix.lower() not in suffixes:
        raise ValueError("unsupported_source")
    if (not resolved.is_file()
            or excluded.intersection(path.parts) or excluded.intersection(resolved.parts)
            or not any(resolved.is_relative_to(root) for root in roots)):
        raise ValueError("unregistered_source_path")
    return resolved


def _read_source_bytes_windows(path: Path, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
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
        buffer = ctypes.create_string_buffer(max_bytes + 1)
        count = wintypes.DWORD()
        if not read(handles[-1], buffer, len(buffer), ctypes.byref(count), None):
            raise win_error(last_error())
        return buffer.raw[:count.value]
    finally:
        for handle in reversed(handles):
            close(handle)


def _read_source_bytes(path: Path, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
    """Open a policy-approved canonical path without re-following any symlinks.

    Pin every ancestor starting at the filesystem root, including the approved
    root itself. A replaced entry is rejected; a directory already opened stays
    pinned even if its name is subsequently replaced. Existing symlink sources
    remain supported by _source_path's canonicalization before this walk.
    """
    if os.name == "nt":
        return (_read_source_bytes_windows(path) if max_bytes == MAX_SOURCE_BYTES else
                _read_source_bytes_windows(path, max_bytes))
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
            return handle.read(max_bytes + 1)
    finally:
        os.close(fd)


def _file(cfg: Config, artifact_id: str, max_bytes: int) -> dict[str, Any]:
    row = index.get_artifact(_db(cfg), artifact_id)
    if row is None:
        raise ValueError("missing_source")
    path = _source_path(cfg, row)
    raw = (_read_source_bytes(path) if max_bytes == MAX_SOURCE_BYTES else
           _read_source_bytes(path, max_bytes))
    if len(raw) > max_bytes:
        raise ValueError("source_too_large")
    text = raw.decode("utf-8")
    if not text.strip():
        raise ValueError("empty_source")
    if row["type"] == "script" and _SECRET_ASSIGNMENT.search(text):
        raise ValueError("credential_source")
    return {"id": artifact_id, "type": row["type"], "locator": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "text": text}


def read_source(cfg: Config, artifact_id: str, *, start_line: int | None = None,
                end_line: int | None = None) -> dict[str, Any]:
    """Hash a complete bounded file; forward only exact approved source lines."""
    excerpt = start_line is not None or end_line is not None
    if excerpt and (type(start_line) is not int or type(end_line) is not int
                    or not 1 <= start_line <= end_line
                    or end_line - start_line >= MAX_EXCERPT_LINES):
        raise ValueError("invalid_read_range")
    source = _file(cfg, artifact_id, MAX_FILE_BYTES if excerpt else MAX_SOURCE_BYTES)
    text = source["text"]
    file_bytes = source["bytes"]
    if excerpt:
        lines = text.splitlines(keepends=True)
        assert start_line is not None and end_line is not None
        if end_line > len(lines):
            raise ValueError("invalid_read_range")
        selected = "".join(lines[start_line - 1:end_line])
        size = len(selected.encode("utf-8"))
        if size > MAX_SOURCE_BYTES:
            raise ValueError("source_too_large")
        source.update(text=selected, bytes=size, start_line=start_line, end_line=end_line,
                      total_lines=len(lines), file_bytes=file_bytes,
                      complete=start_line == 1 and end_line == len(lines))
    return source


def identity(source: dict[str, Any]) -> dict[str, Any]:
    return {**{key: source[key] for key in ("id", "type", "locator", "sha256")},
            **{key: source[key] for key in ("start_line", "end_line", "total_lines", "file_bytes", "complete", "ranges")
               if key in source}}


def reread_source(cfg: Config, source: dict[str, Any]) -> dict[str, Any]:
    if "ranges" not in source:
        return read_source(cfg, source["id"], start_line=source.get("start_line"),
                           end_line=source.get("end_line"))
    ranges = source["ranges"]
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= MAX_RANGES:
        raise ValueError("invalid_read_range")
    current = None
    for item in ranges:
        part = read_source(cfg, source["id"], start_line=item["start_line"], end_line=item["end_line"])
        current = part if current is None else merge_sources(current, part)
    assert current is not None
    return current


def exact_lines(source: dict[str, Any]) -> dict[int, str]:
    if "ranges" in source:
        return source["line_text"]
    return dict(enumerate(source["text"].splitlines(keepends=True), source.get("start_line", 1)))


def merge_sources(previous: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    if any(previous[k] != source[k] for k in ("id", "type", "locator", "sha256")):
        raise ValueError("source_identity_changed")
    if previous.get("complete", True):
        return previous
    if source.get("complete", True):
        return source
    lines = {**exact_lines(previous), **exact_lines(source)}
    bounds: list[dict[str, int]] = []
    for n in sorted(lines):
        if (bounds and n == bounds[-1]["end_line"] + 1
                and n - bounds[-1]["start_line"] < MAX_EXCERPT_LINES):
            bounds[-1]["end_line"] = n
        else:
            bounds.append({"start_line": n, "end_line": n})
    size = sum(len(line.encode("utf-8")) for line in lines.values())
    if len(bounds) > MAX_RANGES or size > MAX_SOURCE_BYTES:
        raise ValueError("source_bytes_budget")
    if len(bounds) == 1:
        return {**source, **bounds[0], "text": "".join(lines[n] for n in sorted(lines)), "bytes": size,
                "complete": len(lines) == source["total_lines"]}
    return {**source, "ranges": bounds, "line_text": lines, "text": "", "bytes": size,
            "start_line": bounds[0]["start_line"], "end_line": bounds[-1]["end_line"],
            "complete": len(lines) == source["total_lines"]}


def locate_source(cfg: Config, artifact_id: str, query: str) -> dict[str, Any]:
    """Literal selected-file navigation, not a summary or independently citable read."""
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError("invalid_location_query")
    source = _file(cfg, artifact_id, MAX_FILE_BYTES)
    matches: list[dict[str, Any]] = []
    truncated = False
    for n, line in enumerate(source["text"].splitlines(), 1):
        if query not in line:
            continue
        if len(matches) == MAX_LOCATIONS:
            truncated = True
            break
        # No body or extracted prose: exact line/column positions only.
        kind = "heading" if line.lstrip().startswith("#") else "symbol" if re.match(
            r"\s*(?:async\s+)?(?:def|class|function)\s", line) else "literal"
        matches.append({"line": n, "column": line.index(query) + 1, "kind": kind})
    return {**identity(source), "file_bytes": source["bytes"],
            "total_lines": len(source["text"].splitlines()), "matches": matches, "truncated": truncated}



def sources_current(cfg: Config, identities: list[dict[str, Any]]) -> bool:
    if not identities or len(identities) > MAX_SOURCES:
        return False
    try:
        return all(identity(reread_source(cfg, item)) == item for item in identities)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


REFUSAL_CODES = frozenset({
    "source_too_large", "empty_source", "missing_source", "unsupported_source",
    "unregistered_source_path", "source_count_budget", "source_bytes_budget",
    "source_unavailable", "unsearched_source", "source_identity_changed",
    "artifact_type_mismatch", "candidate_budget", "source_error",
    "invalid_read_range", "credential_source", "index_root_mismatch",
    "invalid_location_query", "location_budget",
})
ABSTENTION_CATEGORIES = frozenset({
    "unspecified", "insufficient_sources", "ambiguous_lookup", "conflicting_evidence",
    "baseline_coverage", "no_applicable_route",
})


class Diagnostics:
    """Bounded structural receipt, never model prose, search text or source content."""

    def __init__(self) -> None:
        self.attempts: list[dict[str, str]] = []
        self.read_attempts = 0
        self.actions: dict[str, int] = {}
        self.abstention = "unspecified"

    def read(self, artifact_id: str, phase: str, reason: str, *, known: bool) -> None:
        self.read_attempts = min(9999, self.read_attempts + 1)
        if len(self.attempts) < 64:
            # Unknown model-supplied IDs may be arbitrary prose: never retain them.
            self.attempts.append({"id": artifact_id[:600] if known else "",
                                  "phase": phase, "reason": reason})

    def action(self, stage: str, parsed: Any) -> None:
        value = parsed.get("action" if stage == "investigator" else "verdict") if isinstance(parsed, dict) else None
        allowed = ({"search", "read", "read_excerpt", "locate_source", "propose", "unresolved"} if stage == "investigator"
                   else {"applicable", "verified", "unresolved"})
        key = stage + ":" + (value if isinstance(value, str) and value in allowed else "invalid")
        self.actions[key] = min(9999, self.actions.get(key, 0) + 1)
        if stage == "investigator" and value == "unresolved":
            category = parsed.get("category")
            self.abstention = category if isinstance(category, str) and category in ABSTENTION_CATEGORIES else "unspecified"

    def packet(self) -> dict[str, Any]:
        return {"version": 1,
                "read_attempts": self.read_attempts, "attempts": self.attempts,
                "attempts_truncated": self.read_attempts > len(self.attempts),
                "actions": self.actions, "abstention_category": self.abstention}


class Evidence:
    """A single case's in-memory search/read allowance; content is never persisted."""

    def __init__(self, cfg: Config, artifact_type: str, *,
                 diagnostics: Diagnostics | None = None, phase: str = "acquisition") -> None:
        self.diagnostics = diagnostics
        self.phase = phase
        self.cfg = cfg
        self.artifact_type = artifact_type
        self.candidates: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.searches = 0
        self.search_history: list[dict[str, Any]] = []
        self.bytes_read = 0
        self.locations: list[dict[str, Any]] = []
        self.location_attempts = 0
        self.refusals: dict[str, str] = {}

    def search(self, query: str) -> None:
        if self.searches >= 6 or not query.strip() or len(query) > 600:
            raise ValueError("search_budget")
        self.searches += 1
        before = set(self.candidates)
        rows = index.search_index(_db(self.cfg), query, limit=12,
                                  artifact_type=self.artifact_type or None)
        for row in rows:
            if len(self.candidates) >= MAX_CANDIDATES:
                break
            self.candidates[row["id"]] = {key: str(row.get(key, ""))[:600]
                                          for key in ("id", "type", "title", "path", "summary")}
        self.inspect_candidates()
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

        self.inspect_candidates()

    def inspect_candidates(self) -> None:
        """Availability is structural selection advice, never a relevance veto."""
        for artifact_id, candidate in self.candidates.items():
            try:
                path = _source_path(self.cfg, candidate)
                size = path.stat().st_size
                candidate["file_bytes"] = size
                candidate["read_status"] = "excerpt_required" if size > MAX_SOURCE_BYTES else "available"
                if size > MAX_FILE_BYTES:
                    candidate["read_status"] = "source_too_large"
            except (OSError, ValueError) as exc:
                reason = str(exc) if type(exc) is ValueError and str(exc) in REFUSAL_CODES else "source_unavailable"
                candidate["read_status"] = reason

    def read(self, artifact_id: str, *, refresh: bool = False,
             start_line: int | None = None, end_line: int | None = None,
             receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        reason = "read"
        try:
            return self._read(artifact_id, refresh=refresh, start_line=start_line, end_line=end_line, receipt=receipt)
        except (OSError, UnicodeError):
            reason = "source_unavailable"
            raise
        except Exception as exc:
            reason = str(exc) if type(exc) is ValueError and str(exc) in REFUSAL_CODES else "source_error"
            raise
        finally:
            if self.diagnostics is not None:
                self.diagnostics.read(artifact_id, self.phase, reason, known=artifact_id in self.candidates)

    def _read(self, artifact_id: str, *, refresh: bool = False,
              start_line: int | None = None, end_line: int | None = None,
              receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        if artifact_id not in self.candidates:
            raise ValueError("unsearched_source")
        if (artifact_id in self.sources and self.sources[artifact_id].get("complete", True)
                and not refresh and receipt is None and start_line is None and end_line is None):
            return self.sources[artifact_id]
        if artifact_id not in self.sources and len(self.sources) >= MAX_SOURCES:
            raise ValueError("source_count_budget")
        previous_source = self.sources.get(artifact_id)
        if receipt is not None:
            if receipt["id"] != artifact_id:
                raise ValueError("source_identity_changed")
            source = reread_source(self.cfg, receipt)
        elif refresh and previous_source is not None:
            source = reread_source(self.cfg, previous_source)
        else:
            source = read_source(self.cfg, artifact_id, start_line=start_line, end_line=end_line)
        if source["type"] != self.candidates[artifact_id]["type"]:
            raise ValueError("source_identity_changed")
        if any(item["id"] == artifact_id and any(item[k] != source[k] for k in ("locator", "sha256"))
               for item in self.locations):
            raise ValueError("source_identity_changed")
        if previous_source is not None and not refresh and start_line is not None:
            source = merge_sources(previous_source, source)
        # Refreshes replace an existing receipt rather than expanding the packet.
        previous = self.sources.get(artifact_id, {}).get("bytes", 0)
        new_size = self.bytes_read - previous + source["bytes"]
        if new_size > MAX_TOTAL_BYTES:
            raise ValueError("source_bytes_budget")
        self.sources[artifact_id] = source
        self.refusals.pop(artifact_id, None)
        self.bytes_read = new_size
        return source

    def locate(self, artifact_id: str, query: str) -> None:
        if artifact_id not in self.candidates:
            raise ValueError("unsearched_source")
        if self.location_attempts >= 6 or len({item["id"] for item in self.locations} | {artifact_id}) > MAX_SOURCES:
            raise ValueError("location_budget")
        self.location_attempts += 1
        try:
            result = locate_source(self.cfg, artifact_id, query)
            previous = self.sources.get(artifact_id)
            if previous is not None and previous["sha256"] != result["sha256"]:
                raise ValueError("source_identity_changed")
            self.locations.append(result)
        except (OSError, UnicodeError):
            self.refusals[artifact_id] = "source_unavailable"
        except ValueError as exc:
            if str(exc) not in REFUSAL_CODES:
                raise
            self.refusals[artifact_id] = str(exc)

    def packet(self) -> dict[str, Any]:
        return {"locations": self.locations, "search_history": self.search_history,
                "candidates": list(self.candidates.values()), "read_refusals": self.refusals, "sources": [
            {**identity(source), "lines": [f"{i}: {line}" for i, line in
             sorted((n, line.rstrip("\r\n")) for n, line in exact_lines(source).items())]}
            for source in self.sources.values()]}

    def try_read(self, artifact_id: str, *, start_line: int | None = None,
                 end_line: int | None = None) -> bool:
        try:
            self.read(artifact_id, start_line=start_line, end_line=end_line)
            return True
        except (OSError, UnicodeError):
            reason = "source_unavailable"
        except ValueError as exc:
            reason = str(exc)
            if reason not in REFUSAL_CODES:
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
                or not 1 <= start <= end or end - start > 80
                or any(n not in exact_lines(source) for n in range(start, end + 1))):
            raise ValueError("invalid_citation_range")
        checked.append({key: item[key] for key in
                        ("id", "locator", "sha256", "start_line", "end_line")})
    if not set(required_ids).issubset({item["id"] for item in checked}):
        raise ValueError("uncited_route")
    return checked
