"""Format-4 persistence and deterministic search for local knowledge artifacts."""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator, Literal, Sequence, overload

from . import __version__
from .artifacts import Artifact, Edge, build_edges, collect_artifacts
from .config import IndexSettings

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised by platform compatibility checks
    _fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - unavailable on POSIX
    _msvcrt = None  # type: ignore[assignment]

INDEX_FORMAT_VERSION = 4
INDEX_BUILD_LOCK_NAME = "index_build.lock"
INDEX_BUILD_TRANSACTION_LOCK_NAME = "index_build.sqlite"
INDEX_BUILD_LOCK_WAIT_SECONDS = 120.0
SQLITE_REPLACE_ATTEMPTS = 20
SQLITE_REPLACE_RETRY_SECONDS = 0.05
DIRTY_MARKER_NAME = "okf_index_dirty"
FTS_BM25_WEIGHTS = "0.0, 0.2, 6.0, 1.0, 3.0, 2.0, 5.0, 0.4"
_INDEX_BUILD_LOCK_STATE = threading.local()
_LEGACY_INDEX_BUILD_LOCK_FDS: set[int] = set()
_SQLITE_INDEX_BUILD_LOCK_CONNECTIONS: dict[int, sqlite3.Connection] = {}
_INDEX_BUILD_LOCK_RESOURCES_MUTEX = threading.Lock()
_TABLE_SIGNATURES = {
    "artifacts": (
        ("id", "TEXT", 0, 1),
        ("type", "TEXT", 1, 0),
        ("title", "TEXT", 1, 0),
        ("path", "TEXT", 1, 0),
        ("summary", "TEXT", 1, 0),
        ("triggers_json", "TEXT", 1, 0),
        ("entities_json", "TEXT", 1, 0),
        ("related_json", "TEXT", 1, 0),
        ("updated_at", "TEXT", 0, 0),
        ("source", "TEXT", 0, 0),
    ),
    "artifact_fts": (
        ("id", "", 0, 0),
        ("type", "", 0, 0),
        ("title", "", 0, 0),
        ("summary", "", 0, 0),
        ("triggers", "", 0, 0),
        ("entities", "", 0, 0),
        ("path", "", 0, 0),
        ("search_text", "", 0, 0),
    ),
    "edges": (
        ("source", "TEXT", 1, 1),
        ("target", "TEXT", 1, 2),
        ("kind", "TEXT", 1, 3),
        ("evidence", "TEXT", 1, 0),
    ),
    "metadata": (("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)),
}
_QUOTED_QUERY_SPAN_RE = re.compile(r'"(?P<double>[^"\n]+)"|(?<!\w)\'(?P<single>[^\'\n]+)\'(?!\w)')


def _runtime_stopwords() -> set[str]:
    try:
        username = getpass.getuser().strip().lower()
    except Exception:
        return set()
    return {username} if len(username) >= 3 else set()


STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "agent",
    "and",
    "are",
    "before",
    "build",
    "can",
    "code",
    "config",
    "data",
    "default",
    "doc",
    "docs",
    "file",
    "files",
    "for",
    "from",
    "has",
    "have",
    "hermes",
    "into",
    "local",
    "markdown",
    "not",
    "note",
    "repo",
    "review",
    "run",
    "script",
    "server",
    "skill",
    "that",
    "the",
    "this",
    "tool",
    "tools",
    "use",
    "using",
    "when",
    "with",
} | _runtime_stopwords()
QUERY_STOPWORDS = {"find", "flow", "markdown", "need", "next", "show", "want", "what", "where", "which"}
LEGACY_ARTIFACT_TYPE_INTENT: dict[str, set[str]] = {
    "script": {"script"},
    "cron": {"cron_job"},
    "job": {"cron_job"},
    "jobs": {"cron_job"},
    "mcp": {"mcp_server", "script"},
    "wrapper": {"mcp_server", "script"},
}
EXPLICIT_ARTIFACT_TYPE_INTENT: dict[str, set[str]] = {
    "runbook": {"runbook"},
    "skill": {"skill"},
    "doc": {"doc", "skill_support_doc"},
    "docs": {"doc", "skill_support_doc"},
    "document": {"doc", "skill_support_doc"},
    "documentation": {"doc", "skill_support_doc"},
    "reference": {"doc", "skill_support_doc"},
    "memory": {"memory_doc"},
}
ROUTING_HINT_TERMS = STOPWORDS | {"automation", "cron", "job", "runbook", "workflow"}
PROSE_ARTIFACT_TYPES = {"doc", "runbook", "memory_doc"}
STRICT_REFERENCE_TYPES = {"skill", "skill_support_doc"}


class NewerIndexFormatError(RuntimeError):
    """Raised when an older runtime encounters a newer persisted index."""

    def __init__(self, *, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"index format {actual_version} is newer than supported format {expected_version}; "
            "restart or update this older local_knowledge runtime"
        )


def sqlite_readonly_uri(db_path: Path) -> str:
    return f"{db_path.expanduser().resolve().as_uri()}?mode=ro"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(sqlite_readonly_uri(db_path), uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def index_format_version(db_path: Path) -> int | None:
    if not db_path.is_file():
        return None
    try:
        connection = connect_readonly(db_path)
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None
    return int(row[0]) if row is not None else None


def index_format_state(db_path: Path) -> tuple[str, int | None]:
    if not db_path.is_file():
        return "missing", None
    version = index_format_version(db_path)
    if version is None:
        return "corrupt", None
    if version < INDEX_FORMAT_VERSION:
        return "older", version
    if version > INDEX_FORMAT_VERSION:
        return "newer", version
    try:
        _validate_sqlite(db_path)
    except (OSError, sqlite3.Error, ValueError):
        return "corrupt", version
    return "current", version


def index_needs_rebuild(db_path: Path) -> bool:
    state, version = index_format_state(db_path)
    if state == "newer":
        assert version is not None
        raise NewerIndexFormatError(expected_version=INDEX_FORMAT_VERSION, actual_version=version)
    return state in {"missing", "corrupt", "older"}


def _refuse_newer_index(db_path: Path) -> None:
    version = index_format_version(db_path)
    if version is not None and version > INDEX_FORMAT_VERSION:
        raise NewerIndexFormatError(expected_version=INDEX_FORMAT_VERSION, actual_version=version)


def _before_fork() -> None:
    _INDEX_BUILD_LOCK_RESOURCES_MUTEX.acquire()


def _after_fork_in_parent() -> None:
    _INDEX_BUILD_LOCK_RESOURCES_MUTEX.release()


def _after_fork_in_child() -> None:
    global _INDEX_BUILD_LOCK_STATE
    for connection in tuple(_SQLITE_INDEX_BUILD_LOCK_CONNECTIONS.values()):
        try:
            connection.close()
        except sqlite3.Error:
            pass
    _SQLITE_INDEX_BUILD_LOCK_CONNECTIONS.clear()
    for fd in tuple(_LEGACY_INDEX_BUILD_LOCK_FDS):
        try:
            os.close(fd)
        except OSError:
            pass
    _LEGACY_INDEX_BUILD_LOCK_FDS.clear()
    _INDEX_BUILD_LOCK_STATE = threading.local()
    _INDEX_BUILD_LOCK_RESOURCES_MUTEX.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )


def _held_index_build_locks() -> dict[str, int]:
    held = getattr(_INDEX_BUILD_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        setattr(_INDEX_BUILD_LOCK_STATE, "held", held)
    return held


def _open_legacy_index_build_lock(path: Path) -> int:
    with _INDEX_BUILD_LOCK_RESOURCES_MUTEX:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        _LEGACY_INDEX_BUILD_LOCK_FDS.add(fd)
    try:
        if _fcntl is None and os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
            os.fsync(fd)
        return fd
    except BaseException:
        _close_legacy_index_build_lock(fd)
        raise


def _close_legacy_index_build_lock(fd: int) -> None:
    with _INDEX_BUILD_LOCK_RESOURCES_MUTEX:
        if fd not in _LEGACY_INDEX_BUILD_LOCK_FDS:
            return
        try:
            os.close(fd)
        except OSError:
            pass
        _LEGACY_INDEX_BUILD_LOCK_FDS.discard(fd)


def _try_acquire_legacy_index_build_lock(fd: int) -> bool:
    if _fcntl is not None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    if _msvcrt is not None:  # pragma: no cover - Windows-only behavior
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            return False
        return True
    raise RuntimeError("index build locking is unsupported on this platform")


def _release_legacy_index_build_lock(fd: int) -> None:
    if fd not in _LEGACY_INDEX_BUILD_LOCK_FDS:
        return
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        elif _msvcrt is not None:  # pragma: no cover - Windows-only behavior
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    except OSError:
        # A fork-child reset may already have closed this inherited descriptor.
        pass


@contextmanager
def _sqlite_index_build_lock(lock_path: Path) -> Iterator[Path]:
    with _INDEX_BUILD_LOCK_RESOURCES_MUTEX:
        connection = sqlite3.connect(
            str(lock_path),
            timeout=INDEX_BUILD_LOCK_WAIT_SECONDS,
            isolation_level=None,
            check_same_thread=False,
        )
        connection_key = id(connection)
        _SQLITE_INDEX_BUILD_LOCK_CONNECTIONS[connection_key] = connection
    try:
        lock_path.chmod(0o600)
        connection.execute(f"PRAGMA busy_timeout={int(INDEX_BUILD_LOCK_WAIT_SECONDS * 1000)}")
        connection.execute("CREATE TABLE IF NOT EXISTS lock_state (name TEXT PRIMARY KEY)")
        connection.execute("BEGIN EXCLUSIVE")
        yield lock_path
        if _SQLITE_INDEX_BUILD_LOCK_CONNECTIONS.get(connection_key) is connection:
            connection.commit()
    except BaseException:
        if _SQLITE_INDEX_BUILD_LOCK_CONNECTIONS.get(connection_key) is connection:
            connection.rollback()
        raise
    finally:
        with _INDEX_BUILD_LOCK_RESOURCES_MUTEX:
            if _SQLITE_INDEX_BUILD_LOCK_CONNECTIONS.get(connection_key) is connection:
                try:
                    connection.close()
                finally:
                    _SQLITE_INDEX_BUILD_LOCK_CONNECTIONS.pop(connection_key, None)


@contextmanager
def index_build_lock(output_dir: Path) -> Iterator[Path]:
    """Hold the v0.3.12 file gate, then the format-4 SQLite transaction lock."""

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    legacy_lock_path = output_dir / INDEX_BUILD_LOCK_NAME
    sqlite_lock_path = output_dir / INDEX_BUILD_TRANSACTION_LOCK_NAME
    lock_key = str(legacy_lock_path)
    held = _held_index_build_locks()
    if held.get(lock_key, 0):
        held[lock_key] += 1
        try:
            yield sqlite_lock_path
        finally:
            held[lock_key] -= 1
        return

    fd = _open_legacy_index_build_lock(legacy_lock_path)
    deadline = time.monotonic() + INDEX_BUILD_LOCK_WAIT_SECONDS
    legacy_acquired = False
    try:
        while not _try_acquire_legacy_index_build_lock(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for index build lock: {legacy_lock_path}")
            time.sleep(0.05)
        legacy_acquired = True
        with _sqlite_index_build_lock(sqlite_lock_path):
            held[lock_key] = 1
            try:
                yield sqlite_lock_path
            finally:
                held.pop(lock_key, None)
    finally:
        if legacy_acquired:
            _release_legacy_index_build_lock(fd)
        _close_legacy_index_build_lock(fd)


def _dirty_tokens(output_dir: Path) -> tuple[Path, ...]:
    marker = output_dir / DIRTY_MARKER_NAME
    if not marker.is_dir():
        return ()
    return tuple(sorted((path for path in marker.iterdir() if path.is_file()), key=lambda path: path.name))


def _temporary_path(output_dir: Path, name: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=output_dir)
    os.close(descriptor)
    return Path(raw_path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_for_rollback(source: Path, destination: Path) -> None:
    with source.open("rb") as source_handle, destination.open("wb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())


def _write_jsonl(path: Path, artifacts: Sequence[Artifact]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for artifact in artifacts:
            row = asdict(artifact)
            row.pop("search_text", None)
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _build_sqlite(
    path: Path,
    artifacts: Sequence[Artifact],
    edges: Sequence[Edge],
    *,
    source_root: Path,
    build_duration_ms: int,
    jsonl_sha256: str = "",
) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.executescript(
            """
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                path TEXT NOT NULL,
                summary TEXT NOT NULL,
                triggers_json TEXT NOT NULL,
                entities_json TEXT NOT NULL,
                related_json TEXT NOT NULL,
                updated_at TEXT,
                source TEXT
            );
            CREATE VIRTUAL TABLE artifact_fts USING fts5(
                id UNINDEXED,
                type,
                title,
                summary,
                triggers,
                entities,
                path,
                search_text
            );
            CREATE TABLE edges (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                evidence TEXT NOT NULL,
                PRIMARY KEY (source, target, kind)
            );
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO artifacts (
                id, type, title, path, summary, triggers_json, entities_json,
                related_json, updated_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    artifact.id,
                    artifact.type,
                    artifact.title,
                    artifact.path,
                    artifact.summary,
                    json.dumps(artifact.triggers, ensure_ascii=False),
                    json.dumps(artifact.entities, ensure_ascii=False),
                    json.dumps(artifact.related, ensure_ascii=False),
                    artifact.updated_at,
                    artifact.source,
                )
                for artifact in artifacts
            ],
        )
        connection.executemany(
            """
            INSERT INTO artifact_fts (id, type, title, summary, triggers, entities, path, search_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    artifact.id,
                    artifact.type,
                    artifact.title,
                    artifact.summary,
                    " ".join(artifact.triggers),
                    " ".join(artifact.entities),
                    artifact.path,
                    artifact.search_text,
                )
                for artifact in artifacts
            ],
        )
        connection.executemany(
            "INSERT INTO edges (source, target, kind, evidence) VALUES (?, ?, ?, ?)",
            [(edge.source, edge.target, edge.kind, edge.evidence) for edge in edges],
        )
        metadata = {
            "artifact_count": str(len(artifacts)),
            "build_duration_ms": str(build_duration_ms),
            "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "edge_count": str(len(edges)),
            "format_version": str(INDEX_FORMAT_VERSION),
            "jsonl_sha256": jsonl_sha256,
            "plugin_version": __version__,
            "source_root": str(source_root),
        }
        connection.executemany("INSERT INTO metadata (key, value) VALUES (?, ?)", sorted(metadata.items()))
        connection.execute(f"PRAGMA user_version={INDEX_FORMAT_VERSION}")
        connection.commit()
    finally:
        connection.close()


def _validate_jsonl(path: Path, expected_ids: set[str]) -> None:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("JSONL artifact row must be an object")
            rows.append(value)
    ids = [str(row.get("id") or "") for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
        raise ValueError("JSONL artifact IDs do not match the collected corpus")
    if any("search_text" in row for row in rows):
        raise ValueError("JSONL must not expose internal search_text")


def _validate_sqlite(
    path: Path,
    expected_ids: set[str] | None = None,
    expected_edges: int | None = None,
    *,
    jsonl_path: Path | None = None,
) -> None:
    connection = connect_readonly(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise ValueError("SQLite integrity check failed")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != INDEX_FORMAT_VERSION:
            raise ValueError("SQLite index format is incorrect")
        for table_name, expected_signature in _TABLE_SIGNATURES.items():
            signature = tuple(
                (str(row["name"]), str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            )
            if signature != expected_signature:
                raise ValueError(f"SQLite table schema is incorrect: {table_name}")
        fts_definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='artifact_fts'"
        ).fetchone()
        table_types = {
            str(row["name"]): str(row["type"])
            for row in connection.execute("PRAGMA table_list")
        }
        normalized_fts_sql = (
            " ".join(fts_definition[0].split()).casefold()
            if fts_definition is not None and isinstance(fts_definition[0], str)
            else ""
        )
        if (
            table_types.get("artifact_fts") != "virtual"
            or not normalized_fts_sql.startswith("create virtual table artifact_fts using fts5(")
        ):
            raise ValueError("artifact_fts must be an FTS5 virtual table")
        artifact_id_values = [row[0] for row in connection.execute("SELECT id FROM artifacts")]
        fts_id_values = [row[0] for row in connection.execute("SELECT id FROM artifact_fts")]
        if any(not isinstance(value, str) or not value.strip() for value in [*artifact_id_values, *fts_id_values]):
            raise ValueError("SQLite artifact IDs must be non-empty strings")
        artifact_ids = set(artifact_id_values)
        fts_ids = set(fts_id_values)
        artifact_count = int(connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
        fts_count = int(connection.execute("SELECT COUNT(*) FROM artifact_fts").fetchone()[0])
        if artifact_count != len(artifact_ids) or fts_count != len(fts_ids):
            raise ValueError("SQLite artifact IDs must be unique")
        if artifact_ids != fts_ids:
            raise ValueError("SQLite artifact and FTS IDs do not match")
        if expected_ids is not None and artifact_ids != expected_ids:
            raise ValueError("SQLite artifact/FTS IDs do not match the collected corpus")
        edge_count = int(connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
        if expected_edges is not None and edge_count != expected_edges:
            raise ValueError("SQLite edge count does not match the collected graph")
        metadata = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT key, value FROM metadata")
        }
        required_metadata = {
            "artifact_count",
            "build_duration_ms",
            "built_at",
            "edge_count",
            "format_version",
            "jsonl_sha256",
            "plugin_version",
            "source_root",
        }
        if not required_metadata <= metadata.keys():
            raise ValueError("SQLite index is missing required metadata")
        if int(metadata["format_version"]) != INDEX_FORMAT_VERSION:
            raise ValueError("SQLite metadata format is incorrect")
        if int(metadata["artifact_count"]) != artifact_count:
            raise ValueError("SQLite metadata artifact count is incorrect")
        if int(metadata["edge_count"]) != edge_count:
            raise ValueError("SQLite metadata edge count is incorrect")
        expected_jsonl_hash = metadata["jsonl_sha256"]
        companion_path = jsonl_path if jsonl_path is not None else path.with_name("index.jsonl")
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected_jsonl_hash) is None
            or not companion_path.is_file()
            or _sha256_file(companion_path) != expected_jsonl_hash
        ):
            raise ValueError("SQLite metadata does not match the companion JSONL")
        if int(metadata["build_duration_ms"]) < 0:
            raise ValueError("SQLite metadata build duration is incorrect")
        if not metadata["built_at"] or not metadata["plugin_version"] or not metadata["source_root"]:
            raise ValueError("SQLite required metadata values must not be empty")
        dangling = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM edges e
                LEFT JOIN artifacts s ON s.id=e.source
                LEFT JOIN artifacts t ON t.id=e.target
                WHERE s.id IS NULL OR t.id IS NULL
                """
            ).fetchone()[0]
        )
        if dangling:
            raise ValueError("SQLite graph contains dangling edges")
        for row in connection.execute("SELECT triggers_json, entities_json, related_json FROM artifacts"):
            for raw in row:
                try:
                    value = json.loads(raw)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("artifact JSON fields must contain valid JSON") from exc
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError("artifact JSON fields must contain string lists")
    finally:
        connection.close()


def _replace_with_retry(source: Path, destination: Path) -> None:
    for attempt in range(SQLITE_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if attempt + 1 >= SQLITE_REPLACE_ATTEMPTS:
                raise PermissionError(
                    f"failed to publish SQLite index after {SQLITE_REPLACE_ATTEMPTS} attempts; "
                    "the prior authoritative index was not replaced"
                ) from exc
            time.sleep(SQLITE_REPLACE_RETRY_SECONDS)


def _build_locked(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings,
    *,
    collect_artifacts_fn: Callable[..., list[Artifact]],
    build_edges_fn: Callable[[Sequence[Artifact]], list[Edge]],
) -> tuple[list[Artifact], list[Edge]]:
    db_path = output_dir / "index.sqlite"
    jsonl_path = output_dir / "index.jsonl"
    _refuse_newer_index(db_path)
    covered_tokens = _dirty_tokens(output_dir)
    started = time.perf_counter()
    artifacts = sorted(
        collect_artifacts_fn(root, hermes_home, settings, okf_root=output_dir / "okfs"),
        key=lambda artifact: artifact.id,
    )
    edges = sorted(
        build_edges_fn(artifacts),
        key=lambda edge: (edge.source, edge.target, edge.kind, edge.evidence),
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    expected_ids = {artifact.id for artifact in artifacts}
    if len(expected_ids) != len(artifacts):
        raise ValueError("collected artifacts contain duplicate IDs")

    sqlite_temp = _temporary_path(output_dir, "index.sqlite")
    jsonl_temp = _temporary_path(output_dir, "index.jsonl")
    jsonl_backup: Path | None = None
    try:
        _write_jsonl(jsonl_temp, artifacts)
        _validate_jsonl(jsonl_temp, expected_ids)
        jsonl_sha256 = _sha256_file(jsonl_temp)
        _build_sqlite(
            sqlite_temp,
            artifacts,
            edges,
            source_root=root,
            build_duration_ms=duration_ms,
            jsonl_sha256=jsonl_sha256,
        )
        _validate_sqlite(
            sqlite_temp,
            expected_ids,
            len(edges),
            jsonl_path=jsonl_temp,
        )
        _refuse_newer_index(db_path)
        if db_path.is_file() and jsonl_path.is_file():
            jsonl_backup = _temporary_path(output_dir, "index.jsonl.rollback")
            _copy_file_for_rollback(jsonl_path, jsonl_backup)
        os.replace(jsonl_temp, jsonl_path)
        try:
            _replace_with_retry(sqlite_temp, db_path)
        except BaseException:
            if jsonl_backup is not None:
                os.replace(jsonl_backup, jsonl_path)
            else:
                jsonl_path.unlink(missing_ok=True)
            raise
        for token in covered_tokens:
            token.unlink(missing_ok=True)
        return artifacts, edges
    finally:
        sqlite_temp.unlink(missing_ok=True)
        jsonl_temp.unlink(missing_ok=True)
        if jsonl_backup is not None:
            jsonl_backup.unlink(missing_ok=True)


def _build_index_with_dependencies(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
    *,
    force: bool = True,
    acquire_lock: bool = True,
    collect_artifacts_fn: Callable[..., list[Artifact]] | None = None,
    build_edges_fn: Callable[[Sequence[Artifact]], list[Edge]] | None = None,
) -> tuple[list[Artifact], list[Edge]] | None:
    root = root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    hermes_home = hermes_home.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_settings = settings or IndexSettings()
    collector = collect_artifacts_fn or collect_artifacts
    edge_builder = build_edges_fn or build_edges

    def build_once() -> tuple[list[Artifact], list[Edge]] | None:
        if not force and not index_needs_rebuild(output_dir / "index.sqlite") and not _dirty_tokens(output_dir):
            return None
        return _build_locked(
            root,
            output_dir,
            hermes_home,
            resolved_settings,
            collect_artifacts_fn=collector,
            build_edges_fn=edge_builder,
        )

    if not acquire_lock:
        return build_once()
    with index_build_lock(output_dir):
        return build_once()


@overload
def build_index(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
    *,
    force: Literal[True] = True,
) -> tuple[list[Artifact], list[Edge]]: ...


@overload
def build_index(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
    *,
    force: Literal[False],
) -> tuple[list[Artifact], list[Edge]] | None: ...


@overload
def build_index(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
    *,
    force: bool,
) -> tuple[list[Artifact], list[Edge]] | None: ...


def build_index(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
    *,
    force: bool = True,
) -> tuple[list[Artifact], list[Edge]] | None:
    """Collect and publish a validated, recoverable format-4 index pair."""

    return _build_index_with_dependencies(
        root,
        output_dir,
        hermes_home,
        settings,
        force=force,
    )


def artifact_type_counts(artifacts: Sequence[Artifact]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for artifact in artifacts:
        counts[artifact.type] = counts.get(artifact.type, 0) + 1
    return dict(sorted(counts.items()))


def _utc_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def index_metadata(db_path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {"index_exists": db_path.exists()}
    if not db_path.exists():
        return metadata
    try:
        stat_result = db_path.stat()
        metadata.update(
            {
                "index_mtime": _utc_from_timestamp(stat_result.st_mtime),
                "index_age_seconds": max(0, int(time.time() - stat_result.st_mtime)),
            }
        )
        connection = connect_readonly(db_path)
        try:
            counts = {
                str(row[0]): int(row[1])
                for row in connection.execute("SELECT type, COUNT(*) FROM artifacts GROUP BY type")
            }
            persisted_metadata = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT key, value FROM metadata WHERE key IN ('jsonl_sha256', 'format_version')"
                )
            }
            metadata.update(
                {
                    "artifact_count": sum(counts.values()),
                    "artifact_counts_by_type": dict(sorted(counts.items())),
                    "edge_count": int(connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]),
                    "index_format_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                    "jsonl_sha256": persisted_metadata.get("jsonl_sha256"),
                }
            )
        finally:
            connection.close()
    except Exception as exc:
        metadata["index_metadata_error"] = f"sqlite stats failed: {type(exc).__name__}: {exc}"
    return metadata


def index_source_root(db_path: Path) -> str | None:
    """Read the source root persisted in one current index, if available."""

    if not db_path.is_file():
        return None
    try:
        connection = connect_readonly(db_path)
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='source_root'"
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None
    return str(row[0]) if row is not None else None


def decode_artifact_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    for field in ("type_priority", "metadata_score", "source_tier", "strict_candidate", "search_text"):
        output.pop(field, None)
    for field_name in ("triggers_json", "entities_json", "related_json"):
        new_name = field_name.removesuffix("_json")
        try:
            value = json.loads(output.pop(field_name))
        except (KeyError, TypeError, json.JSONDecodeError):
            value = []
        output[new_name] = value if isinstance(value, list) else []
    return output


def get_artifact(db_path: Path, artifact_id: str) -> dict[str, Any] | None:
    connection = connect_readonly(db_path)
    try:
        row = connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return decode_artifact_row(row) if row else None
    finally:
        connection.close()


def get_neighbors(db_path: Path, artifact_id: str) -> list[dict[str, Any]]:
    connection = connect_readonly(db_path)
    try:
        rows = connection.execute(
            """
            SELECT e.kind, e.evidence, a.*
            FROM edges e JOIN artifacts a ON a.id=e.target
            WHERE e.source=?
            UNION ALL
            SELECT e.kind, e.evidence, a.*
            FROM edges e JOIN artifacts a ON a.id=e.source
            WHERE e.target=?
            ORDER BY kind, title
            """,
            (artifact_id, artifact_id),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = decode_artifact_row(row)
            item["edge_kind"] = item.pop("kind")
            item["edge_evidence"] = item.pop("evidence")
            output.append(item)
        return output
    finally:
        connection.close()


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _normalize_query_term(term: str) -> str:
    term = term.lower().strip()
    if len(term) > 4 and term.endswith("ies"):
        return f"{term[:-3]}y"
    if len(term) > 4 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def _raw_query_terms(query: str) -> list[str]:
    return [
        _normalize_query_term(term)
        for term in re.findall(r"[A-Za-z0-9]{2,}", query.lower())
    ]


def _query_terms(query: str, *, drop_stopwords: bool = True) -> list[str]:
    terms = _raw_query_terms(query)
    if drop_stopwords:
        terms = [term for term in terms if term not in QUERY_STOPWORDS]
    return _unique(terms)


def _fts_query(query: str, *, operator: str = "AND") -> str:
    expressions: list[str] = []
    unquoted: set[str] = set()

    def append_unquoted(value: str) -> None:
        for term in _query_terms(value):
            if term not in unquoted:
                unquoted.add(term)
                expressions.append(f"{term}*")

    cursor = 0
    for match in _QUOTED_QUERY_SPAN_RE.finditer(query):
        append_unquoted(query[cursor : match.start()])
        phrase = match.group("double") if match.group("double") is not None else match.group("single")
        phrase_terms = re.findall(r"[A-Za-z0-9]+", phrase.lower())
        if phrase_terms:
            expressions.append(f'"{" ".join(phrase_terms)}"')
        cursor = match.end()
    append_unquoted(query[cursor:])
    return (" OR " if operator.upper() == "OR" else " ").join(expressions)


def _has_quoted_phrase(query: str) -> bool:
    return bool(_QUOTED_QUERY_SPAN_RE.search(query))


def _is_quoted_only_query(query: str) -> bool:
    return _has_quoted_phrase(query) and not _query_terms(_QUOTED_QUERY_SPAN_RE.sub(" ", query))


def _high_signal_terms(terms: Sequence[str]) -> list[str]:
    specific = [term for term in terms if term not in ROUTING_HINT_TERMS]
    return specific or list(terms)


def _specific_terms_for_ranking(
    terms: Sequence[str],
    active_intent_terms: set[str],
    *,
    explicit_type_intent: bool,
) -> list[str]:
    source = (
        [term for term in terms if term not in ROUTING_HINT_TERMS]
        if explicit_type_intent
        else _high_signal_terms(terms)
    )
    return [term for term in source if term not in active_intent_terms]


def _token_hits(tokens: set[str], terms: Sequence[str]) -> int:
    return sum(any(token == term or token.startswith(term) for token in tokens) for term in terms)


def _portable_basename(value: str) -> str:
    clean = str(value).rstrip("`.,);]")
    path_type = PureWindowsPath if "\\" in clean else PurePosixPath
    return path_type(clean).name


def _identity_match_tier(row: dict[str, Any], terms: Sequence[str]) -> int:
    if not terms:
        return 3
    artifact_id = str(row.get("id") or "")
    title = str(row.get("title") or "")
    path = str(row.get("path") or "")
    path_name = _portable_basename(path)
    path_stem = Path(path_name).stem
    compact = "".join(terms)
    if len(terms) >= 2 and compact:
        for value in (artifact_id, title, path_name, path_stem):
            if "".join(_query_terms(value, drop_stopwords=False)) == compact:
                return 0
    identity_terms = [term for term in terms if term not in ROUTING_HINT_TERMS]
    if len(identity_terms) < 2:
        return 3
    title_tokens = set(_query_terms(f"{artifact_id} {title}", drop_stopwords=False))
    basename_tokens = set(_query_terms(f"{path_name} {path_stem}", drop_stopwords=False))
    path_tokens = set(_query_terms(path, drop_stopwords=False))
    if _token_hits(title_tokens, identity_terms) == len(identity_terms):
        return 1
    if _token_hits(basename_tokens, identity_terms) == len(identity_terms):
        return 1
    if _token_hits(path_tokens, identity_terms) == len(identity_terms):
        return 2
    return 3


def _type_priority(artifact_type: str) -> int:
    return {
        "skill": 0,
        "script": 1,
        "cron_job": 2,
        "mcp_server": 3,
        "memory_doc": 4,
        "runbook": 5,
        "tool_okf": 6,
    }.get(artifact_type, 7)


def _requested_operational_types(terms: Sequence[str]) -> set[str]:
    requested: set[str] = set()
    for term in terms:
        requested.update(LEGACY_ARTIFACT_TYPE_INTENT.get(term, ()))
    explicit_terms = {term for term in terms if term in EXPLICIT_ARTIFACT_TYPE_INTENT}
    terminal_term = terms[-1] if terms else ""
    if not requested and explicit_terms == {terminal_term}:
        requested.update(EXPLICIT_ARTIFACT_TYPE_INTENT[terminal_term])
    return requested


def _legacy_requested_operational_types(terms: Sequence[str]) -> set[str]:
    requested: set[str] = set()
    for term in terms:
        requested.update(LEGACY_ARTIFACT_TYPE_INTENT.get(term, ()))
    return requested


def _operational_intent_terms(terms: Sequence[str]) -> set[str]:
    """Return only type terms that actually activated deterministic promotion."""

    active = {term for term in terms if term in LEGACY_ARTIFACT_TYPE_INTENT}
    explicit_terms = {term for term in terms if term in EXPLICIT_ARTIFACT_TYPE_INTENT}
    terminal_term = terms[-1] if terms else ""
    if not active and explicit_terms == {terminal_term}:
        active.add(terminal_term)
    return active


def _row_specific_hits(row: dict[str, Any], specific_terms: Sequence[str]) -> int:
    source = " ".join(
        [
            str(row.get("id") or ""),
            str(row.get("title") or ""),
            str(row.get("path") or ""),
            str(row.get("summary") or ""),
            " ".join(row.get("triggers") or []),
            " ".join(row.get("entities") or []),
        ]
    )
    return _token_hits(set(_query_terms(source, drop_stopwords=False)), specific_terms)


def _row_entity_hits(row: dict[str, Any], specific_terms: Sequence[str]) -> int:
    source = " ".join(row.get("entities") or [])
    return _token_hits(set(_query_terms(source, drop_stopwords=False)), specific_terms)


@dataclass(frozen=True)
class _Candidate:
    row: dict[str, Any]
    source_tier: int
    strict: bool


def _operational_tier(candidate: _Candidate, requested: set[str], specific_terms: Sequence[str]) -> int:
    if not requested:
        return 0
    row = candidate.row
    artifact_type = str(row.get("type") or "")
    protect_reference = requested == {"script"}
    hits = _row_specific_hits(row, specific_terms)
    matches_all = not specific_terms or hits == len(specific_terms)
    matches_any = not specific_terms or hits > 0
    if protect_reference and candidate.strict and artifact_type in STRICT_REFERENCE_TYPES:
        return 0
    if artifact_type in requested:
        if protect_reference and matches_any:
            return 1
        if not protect_reference and matches_all:
            return 1
    if candidate.strict and artifact_type in STRICT_REFERENCE_TYPES and matches_all:
        return 2
    if artifact_type in PROSE_ARTIFACT_TYPES:
        return 3
    return 4


def _rank_key(
    candidate: _Candidate,
    terms: Sequence[str],
    requested_types: set[str],
    specific_terms: Sequence[str],
) -> tuple[Any, ...]:
    """Return the one deterministic ordering key used for every candidate."""

    row = candidate.row
    high_signal = _high_signal_terms(terms)
    title_tokens = set(_query_terms(f"{row.get('id', '')} {row.get('title', '')}", drop_stopwords=False))
    path_tokens = set(_query_terms(str(row.get("path") or ""), drop_stopwords=False))
    trigger_tokens = set(_query_terms(" ".join(row.get("triggers") or []), drop_stopwords=False))
    summary_tokens = set(_query_terms(str(row.get("summary") or ""), drop_stopwords=False))
    entity_tokens = set(_query_terms(" ".join(row.get("entities") or []), drop_stopwords=False))
    title_hits = _token_hits(title_tokens, terms)
    return (
        _operational_tier(candidate, requested_types, specific_terms),
        candidate.source_tier,
        _identity_match_tier(row, terms),
        0 if terms and title_hits == len(terms) else 1,
        -_token_hits(title_tokens, high_signal),
        -_token_hits(trigger_tokens, high_signal),
        -_token_hits(summary_tokens, high_signal),
        -_token_hits(path_tokens, high_signal),
        -title_hits,
        -_token_hits(trigger_tokens, terms),
        -_token_hits(summary_tokens, terms),
        -_token_hits(path_tokens, terms),
        -_token_hits(entity_tokens, terms),
        _type_priority(str(row.get("type") or "")),
        float(row.get("rank") or 0.0),
        str(row.get("title") or ""),
        str(row.get("id") or ""),
    )


def _query_fts_rows(
    connection: sqlite3.Connection,
    match: str,
    candidate_limit: int,
    artifact_type: str,
) -> list[sqlite3.Row]:
    where = "artifact_fts MATCH ?"
    params: list[Any] = [match]
    if artifact_type:
        where += " AND a.type=?"
        params.append(artifact_type)
    params.append(candidate_limit)
    return connection.execute(
        f"""
        SELECT a.*, bm25(artifact_fts, {FTS_BM25_WEIGHTS}) AS rank,
               CASE a.type
                 WHEN 'skill' THEN 0
                 WHEN 'script' THEN 1
                 WHEN 'cron_job' THEN 2
                 WHEN 'mcp_server' THEN 3
                 WHEN 'memory_doc' THEN 4
                 WHEN 'runbook' THEN 5
                 WHEN 'tool_okf' THEN 6
                 ELSE 7
               END AS type_priority
        FROM artifact_fts JOIN artifacts a ON a.id=artifact_fts.id
        WHERE {where}
        ORDER BY rank, type_priority, a.title, a.id
        LIMIT ?
        """,
        params,
    ).fetchall()


def _query_identity_rows(
    connection: sqlite3.Connection,
    terms: Sequence[str],
    candidate_limit: int,
    artifact_type: str,
) -> list[sqlite3.Row]:
    identity_terms = [term for term in terms if term not in ROUTING_HINT_TERMS]
    if len(identity_terms) < 2:
        return []
    fields = ("a.id", "a.title", "a.path")
    clauses: list[str] = []
    where_params: list[Any] = []
    score_parts: list[str] = []
    score_params: list[Any] = []
    for term in identity_terms:
        like = f"%{term.lower()}%"
        clauses.append("(" + " OR ".join(f"lower({field}) LIKE ?" for field in fields) + ")")
        where_params.extend([like] * len(fields))
        for field, weight in (("a.id", 5), ("a.title", 5), ("a.path", 4)):
            score_parts.append(f"CASE WHEN lower({field}) LIKE ? THEN {weight} ELSE 0 END")
            score_params.append(like)
    where = " AND ".join(clauses)
    if artifact_type:
        where += " AND a.type=?"
        where_params.append(artifact_type)
    where_params.append(candidate_limit)
    return connection.execute(
        f"""
        SELECT a.*, 0.0 AS rank, ({" + ".join(score_parts)}) AS metadata_score,
               CASE a.type
                 WHEN 'skill' THEN 0 WHEN 'script' THEN 1 WHEN 'cron_job' THEN 2
                 WHEN 'mcp_server' THEN 3 WHEN 'memory_doc' THEN 4 WHEN 'runbook' THEN 5
                 WHEN 'tool_okf' THEN 6 ELSE 7
               END AS type_priority
        FROM artifacts a
        WHERE {where}
        ORDER BY metadata_score DESC, type_priority, a.title, a.id
        LIMIT ?
        """,
        [*score_params, *where_params],
    ).fetchall()


def _query_metadata_rows(
    connection: sqlite3.Connection,
    terms: Sequence[str],
    candidate_limit: int,
    artifact_type: str,
) -> list[sqlite3.Row]:
    candidate_terms = [term for term in _high_signal_terms(terms) if term][:8]
    if not candidate_terms:
        return []
    field_weights = (
        ("a.id", 5),
        ("a.title", 5),
        ("a.path", 4),
        ("a.triggers_json", 2),
        ("a.entities_json", 2),
        ("a.summary", 1),
    )
    fields = tuple(field for field, _weight in field_weights)
    clauses: list[str] = []
    where_params: list[Any] = []
    score_parts: list[str] = []
    score_params: list[Any] = []
    for term in candidate_terms:
        like = f"%{term.lower()}%"
        clauses.append("(" + " OR ".join(f"lower({field}) LIKE ?" for field in fields) + ")")
        where_params.extend([like] * len(fields))
        for field, weight in field_weights:
            score_parts.append(f"CASE WHEN lower({field}) LIKE ? THEN {weight} ELSE 0 END")
            score_params.append(like)
    where = "(" + " OR ".join(clauses) + ")"
    if artifact_type:
        where += " AND a.type=?"
        where_params.append(artifact_type)
    where_params.append(candidate_limit)
    return connection.execute(
        f"""
        SELECT a.*, 0.0 AS rank, ({" + ".join(score_parts)}) AS metadata_score,
               CASE a.type
                 WHEN 'skill' THEN 0 WHEN 'script' THEN 1 WHEN 'cron_job' THEN 2
                 WHEN 'mcp_server' THEN 3 WHEN 'memory_doc' THEN 4 WHEN 'runbook' THEN 5
                 WHEN 'tool_okf' THEN 6 ELSE 7
               END AS type_priority
        FROM artifacts a
        WHERE {where}
        ORDER BY metadata_score DESC, type_priority, a.title, a.id
        LIMIT ?
        """,
        [*score_params, *where_params],
    ).fetchall()


def _support_parent(row: dict[str, Any]) -> str | None:
    if row.get("type") != "skill_support_doc":
        return None
    return next((str(value) for value in row.get("related") or [] if str(value).startswith("skill:")), None)


def _fetch_parents(
    connection: sqlite3.Connection,
    artifact_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not artifact_ids:
        return {}
    placeholders = ", ".join("?" for _ in artifact_ids)
    rows = connection.execute(
        f"SELECT a.*, 0.0 AS rank FROM artifacts a WHERE a.id IN ({placeholders})",
        list(artifact_ids),
    ).fetchall()
    return {str(row["id"]): decode_artifact_row(row) for row in rows}


def _select_candidates(
    candidates: Sequence[_Candidate], *, enforce_support_diversity: bool = True
) -> list[_Candidate]:
    selected: list[_Candidate] = []
    emitted: set[str] = set()
    support_counts: dict[str, int] = {}
    for candidate in candidates:
        artifact_id = str(candidate.row.get("id") or "")
        if artifact_id in emitted:
            continue
        parent = _support_parent(candidate.row)
        if parent and enforce_support_diversity:
            count = support_counts.get(parent, 0)
            if count >= 1:
                continue
            support_counts[parent] = count + 1
        selected.append(candidate)
        emitted.add(artifact_id)
    return selected


def _rank_group(
    connection: sqlite3.Connection,
    candidates: Sequence[_Candidate],
    terms: Sequence[str],
    requested_types: set[str],
    specific_terms: Sequence[str],
    *,
    lift_parents: bool,
    enforce_support_diversity: bool,
) -> list[_Candidate]:
    ordered = sorted(
        candidates,
        key=lambda candidate: _rank_key(candidate, terms, requested_types, specific_terms),
    )
    if not lift_parents:
        return _select_candidates(
            ordered,
            enforce_support_diversity=enforce_support_diversity,
        )
    existing = {str(candidate.row.get("id") or ""): candidate for candidate in ordered}
    missing_parent_ids = list(
        dict.fromkeys(
            parent
            for candidate in ordered
            if (parent := _support_parent(candidate.row)) and parent not in existing
        )
    )
    parents = _fetch_parents(connection, missing_parent_ids)
    expanded: list[_Candidate] = []
    for candidate in ordered:
        parent = _support_parent(candidate.row)
        if parent:
            parent_candidate = existing.get(parent)
            if parent_candidate is None:
                parent_row = parents.get(parent)
                if parent_row is not None:
                    parent_candidate = _Candidate(
                        parent_row,
                        source_tier=candidate.source_tier,
                        strict=candidate.strict,
                    )
            if parent_candidate is not None:
                expanded.append(parent_candidate)
        expanded.append(candidate)
    return _select_candidates(
        expanded,
        enforce_support_diversity=enforce_support_diversity,
    )


def _explicit_identity_terms(
    candidates: Sequence[_Candidate],
    requested_types: set[str],
    specific_terms: Sequence[str],
) -> tuple[set[str], str | None]:
    families_by_term: dict[str, set[str]] = {term: set() for term in specific_terms}
    for candidate in candidates:
        row = candidate.row
        if str(row.get("type") or "") not in requested_types:
            continue
        entity_terms = set(
            _query_terms(" ".join(row.get("entities") or []), drop_stopwords=False)
        )
        family = _support_parent(row) or str(row.get("id") or "")
        for term in specific_terms:
            if term in entity_terms:
                families_by_term[term].add(family)
    unique_families = {
        term: next(iter(families))
        for term, families in families_by_term.items()
        if len(families) == 1
    }
    if len(set(unique_families.values())) != 1:
        return set(), None
    return set(unique_families), next(iter(unique_families.values()))


def _promote_explicit_type_candidate(
    selected: list[_Candidate],
    requested_types: set[str],
    specific_terms: Sequence[str],
) -> list[_Candidate]:
    """Move one strong explicit-type match forward without crowding out its baseline neighbors."""

    if len(specific_terms) < 2:
        return selected
    identity_terms, identity_family = _explicit_identity_terms(
        selected,
        requested_types,
        specific_terms,
    )
    if not identity_terms or identity_family is None:
        return selected
    target = next(
        (
            candidate
            for candidate in selected
            if str(candidate.row.get("type") or "") in requested_types
            and (
                _support_parent(candidate.row)
                or str(candidate.row.get("id") or "")
            )
            == identity_family
            and _row_specific_hits(candidate.row, specific_terms) == len(specific_terms)
            and _row_entity_hits(candidate.row, sorted(identity_terms)) > 0
            and _row_entity_hits(candidate.row, specific_terms) < len(specific_terms)
        ),
        None,
    )
    if target is None:
        return selected

    promoted = [target]
    parent_id = _support_parent(target.row)
    if parent_id:
        parent = next(
            (candidate for candidate in selected if str(candidate.row.get("id") or "") == parent_id),
            None,
        )
        if parent is not None:
            promoted.insert(0, parent)
    promoted_ids = {str(candidate.row.get("id") or "") for candidate in promoted}
    remaining = [
        candidate
        for candidate in selected
        if str(candidate.row.get("id") or "") not in promoted_ids
    ]
    return [*promoted, *remaining]


def _finalize_candidates(
    candidates: Sequence[_Candidate],
    requested_types: set[str],
    specific_terms: Sequence[str],
    explicit_type_intent: bool,
    output_limit: int,
    *,
    baseline_output: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if explicit_type_intent:
        promotion_pool = list(candidates)
        promoted = _promote_explicit_type_candidate(
            promotion_pool, requested_types, specific_terms
        )
        if promoted is promotion_pool and baseline_output is not None:
            return list(baseline_output[:output_limit])
        selected = _select_candidates(promoted)
    else:
        selected = _select_candidates(candidates)
    if not explicit_type_intent and requested_types:
        selected.sort(
            key=lambda candidate: _operational_tier(
                candidate,
                requested_types,
                specific_terms,
            )
        )
    return [candidate.row for candidate in selected[:output_limit]]


def _decode_candidates(
    rows: Sequence[sqlite3.Row],
    *,
    source_tier: int,
    strict: bool,
    excluded_ids: set[str] | None = None,
) -> list[_Candidate]:
    excluded = excluded_ids or set()
    output: list[_Candidate] = []
    seen: set[str] = set()
    for row in rows:
        decoded = decode_artifact_row(row)
        artifact_id = str(decoded.get("id") or "")
        if artifact_id in excluded or artifact_id in seen:
            continue
        seen.add(artifact_id)
        output.append(_Candidate(decoded, source_tier=source_tier, strict=strict))
    return output


def search_index(
    db_path: Path,
    query: str,
    *,
    limit: int = 10,
    artifact_type: str | None = None,
    _disable_explicit_intent: bool = False,
) -> list[dict[str, Any]]:
    output_limit = max(0, int(limit))
    if output_limit == 0:
        return []
    terms = _query_terms(query)
    intent_terms = _raw_query_terms(query)
    strict_match = _fts_query(query)
    if not strict_match:
        return []
    fallback_match = _fts_query(query, operator="OR")
    type_filter = str(artifact_type or "").strip()
    exact_query = _has_quoted_phrase(query)
    quoted_only = _is_quoted_only_query(query)
    lift_parents = not exact_query and not type_filter
    requested = (
        set()
        if exact_query
        else (
            _legacy_requested_operational_types(intent_terms)
            if _disable_explicit_intent
            else _requested_operational_types(intent_terms)
        )
    )
    active_intent_terms = (
        {
            term
            for term in intent_terms
            if term in LEGACY_ARTIFACT_TYPE_INTENT
        }
        if _disable_explicit_intent
        else _operational_intent_terms(intent_terms)
    )
    explicit_type_intent = not type_filter and not _disable_explicit_intent and any(
        term in EXPLICIT_ARTIFACT_TYPE_INTENT for term in active_intent_terms
    )
    baseline_output = (
        search_index(
            db_path,
            query,
            limit=output_limit,
            artifact_type=artifact_type,
            _disable_explicit_intent=True,
        )
        if explicit_type_intent
        else None
    )
    ranking_requested = set() if explicit_type_intent else requested
    specific_terms = _specific_terms_for_ranking(
        terms,
        active_intent_terms,
        explicit_type_intent=explicit_type_intent,
    )
    candidate_limit = max(output_limit * 20, 100)

    connection = connect_readonly(db_path)
    try:
        strict = _rank_group(
            connection,
            _decode_candidates(
                _query_fts_rows(connection, strict_match, candidate_limit, type_filter),
                source_tier=0 if exact_query else 1,
                strict=True,
            ),
            terms,
            ranking_requested,
            specific_terms,
            lift_parents=lift_parents,
            enforce_support_diversity=not explicit_type_intent,
        )
        strict_ids = {str(candidate.row.get("id") or "") for candidate in strict}
        if quoted_only:
            return _finalize_candidates(
                strict,
                requested,
                specific_terms,
                explicit_type_intent,
                output_limit,
            )

        identity: list[_Candidate] = []
        if not requested or explicit_type_intent:
            identity_candidates = _decode_candidates(
                _query_identity_rows(connection, terms, candidate_limit, type_filter),
                source_tier=1 if exact_query else 0,
                strict=False,
                excluded_ids=strict_ids,
            )
            identity = _rank_group(
                connection,
                [
                    candidate
                    for candidate in identity_candidates
                    if _identity_match_tier(candidate.row, terms) < 3
                ],
                terms,
                ranking_requested,
                specific_terms,
                lift_parents=lift_parents,
                enforce_support_diversity=not explicit_type_intent,
            )
        identity_ids = {str(candidate.row.get("id") or "") for candidate in identity}
        prioritized = [*strict, *identity] if exact_query else [*identity, *strict]
        if len(strict) >= output_limit and not requested:
            return _finalize_candidates(
                prioritized,
                requested,
                specific_terms,
                explicit_type_intent,
                output_limit,
            )

        fallback_rows: list[sqlite3.Row] = []
        if fallback_match != strict_match:
            fallback_rows.extend(_query_fts_rows(connection, fallback_match, candidate_limit, type_filter))
        if not identity:
            fallback_rows.extend(_query_metadata_rows(connection, terms, candidate_limit, type_filter))
        fallback = _rank_group(
            connection,
            _decode_candidates(
                fallback_rows,
                source_tier=2,
                strict=False,
                excluded_ids=strict_ids | identity_ids,
            ),
            terms,
            ranking_requested,
            specific_terms,
            lift_parents=lift_parents,
            enforce_support_diversity=not explicit_type_intent,
        )
        return _finalize_candidates(
            [*prioritized, *fallback],
            requested,
            specific_terms,
            explicit_type_intent,
            output_limit,
            baseline_output=baseline_output,
        )
    finally:
        connection.close()
