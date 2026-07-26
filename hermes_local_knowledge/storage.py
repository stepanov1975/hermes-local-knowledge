"""Persistence for local knowledge JSONL and SQLite indexes."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .models import Artifact, Edge, IndexSettings

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised by the import compatibility test
    _fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - unavailable on POSIX
    _msvcrt = None  # type: ignore[assignment]

INDEX_BUILD_LOCK_NAME = "index_build.lock"
INDEX_BUILD_LOCK_WAIT_SECONDS = 120.0
INDEX_FORMAT_VERSION = 2
SQLITE_REPLACE_ATTEMPTS = 20
SQLITE_REPLACE_RETRY_SECONDS = 0.05
_INDEX_BUILD_LOCK_STATE = threading.local()
_INDEX_BUILD_LOCK_FDS: set[int] = set()
_INDEX_BUILD_LOCK_FDS_MUTEX = threading.Lock()


class NewerIndexFormatError(RuntimeError):
    """Raised when an older runtime encounters a newer persisted index."""

    def __init__(self, *, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"index format {actual_version} is newer than supported format {expected_version}; "
            "restart or update this older local_knowledge runtime"
        )


def _before_fork() -> None:
    _INDEX_BUILD_LOCK_FDS_MUTEX.acquire()


def _after_fork_in_parent() -> None:
    _INDEX_BUILD_LOCK_FDS_MUTEX.release()


def _after_fork_in_child() -> None:
    global _INDEX_BUILD_LOCK_STATE
    for fd in tuple(_INDEX_BUILD_LOCK_FDS):
        try:
            os.close(fd)
        except OSError:
            pass
    _INDEX_BUILD_LOCK_FDS.clear()
    _INDEX_BUILD_LOCK_STATE = threading.local()
    _INDEX_BUILD_LOCK_FDS_MUTEX.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )


def index_build_lock_path(output_dir: Path) -> Path:
    return output_dir.expanduser().resolve() / INDEX_BUILD_LOCK_NAME


def _held_index_build_locks() -> dict[str, int]:
    held = getattr(_INDEX_BUILD_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        setattr(_INDEX_BUILD_LOCK_STATE, "held", held)
    return held


def _open_index_build_lock(path: Path) -> int:
    with _INDEX_BUILD_LOCK_FDS_MUTEX:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        _INDEX_BUILD_LOCK_FDS.add(fd)
    if _fcntl is None and os.fstat(fd).st_size < 1:
        os.write(fd, b"\0")
        os.fsync(fd)
    return fd


def _close_index_build_lock(fd: int) -> None:
    with _INDEX_BUILD_LOCK_FDS_MUTEX:
        if fd not in _INDEX_BUILD_LOCK_FDS:
            return
        try:
            os.close(fd)
        except OSError:
            pass
        _INDEX_BUILD_LOCK_FDS.discard(fd)


def _try_acquire_index_build_lock(fd: int) -> bool:
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


def _release_index_build_lock(fd: int) -> None:
    if fd not in _INDEX_BUILD_LOCK_FDS:
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
def index_build_lock(output_dir: Path) -> Iterator[Path]:
    lock_path = index_build_lock_path(output_dir)
    lock_key = str(lock_path)
    held = _held_index_build_locks()
    if held.get(lock_key, 0):
        held[lock_key] += 1
        try:
            yield lock_path
        finally:
            held[lock_key] -= 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open_index_build_lock(lock_path)
    deadline = time.monotonic() + INDEX_BUILD_LOCK_WAIT_SECONDS
    try:
        while not _try_acquire_index_build_lock(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for index build lock: {lock_path}")
            time.sleep(0.05)
        payload = json.dumps({"pid": os.getpid(), "acquired_at": time.time()}).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.ftruncate(fd, max(1, len(payload)))
        os.fsync(fd)
        held[lock_key] = 1
        try:
            yield lock_path
        finally:
            held.pop(lock_key, None)
            _release_index_build_lock(fd)
    finally:
        _close_index_build_lock(fd)


def write_jsonl(path: Path, artifacts: Sequence[Artifact]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False)
    temp_path = Path(temp_file.name)
    temp_file.close()
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            for artifact in artifacts:
                row = asdict(artifact)
                row.pop("search_text", None)
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _refuse_newer_index(path: Path) -> None:
    version = index_format_version(path)
    if version is not None and version > INDEX_FORMAT_VERSION:
        raise NewerIndexFormatError(
            expected_version=INDEX_FORMAT_VERSION,
            actual_version=version,
        )


def _replace_sqlite_with_retry(source: Path, destination: Path) -> None:
    for attempt in range(SQLITE_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if attempt + 1 >= SQLITE_REPLACE_ATTEMPTS:
                raise PermissionError(
                    f"failed to publish SQLite index after {SQLITE_REPLACE_ATTEMPTS} attempts; "
                    "the destination index was not replaced and may still be held by an active reader"
                ) from exc
            time.sleep(SQLITE_REPLACE_RETRY_SECONDS)


def build_sqlite(path: Path, artifacts: Sequence[Artifact], edges: Sequence[Edge]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_newer_index(path)
    temp_file = tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False)
    temp_path = Path(temp_file.name)
    temp_file.close()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(temp_path))
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(
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
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE artifact_fts USING fts5(
                id UNINDEXED,
                type,
                title,
                summary,
                triggers,
                entities,
                path,
                search_text
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE edges (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                evidence TEXT NOT NULL,
                PRIMARY KEY (source, target, kind)
            )
            """
        )
        conn.executemany(
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
        conn.executemany(
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
        conn.executemany(
            "INSERT INTO edges (source, target, kind, evidence) VALUES (?, ?, ?, ?)",
            [(edge.source, edge.target, edge.kind, edge.evidence) for edge in edges],
        )
        conn.execute(f"PRAGMA user_version = {INDEX_FORMAT_VERSION}")
        conn.commit()
        conn.close()
        _replace_sqlite_with_retry(temp_path, path)
    finally:
        try:
            if conn is not None:
                conn.close()
        finally:
            temp_path.unlink(missing_ok=True)


def _publish_index_generation(
    sqlite_path: Path,
    jsonl_path: Path,
    artifacts: Sequence[Artifact],
    edges: Sequence[Edge],
    *,
    build_sqlite_fn: Callable[[Path, Sequence[Artifact], Sequence[Edge]], None],
    write_jsonl_fn: Callable[[Path, Sequence[Artifact]], None],
) -> None:
    backup_path: Path | None = None
    if sqlite_path.exists():
        backup_file = tempfile.NamedTemporaryFile(
            prefix=f".{sqlite_path.name}.", suffix=".bak", dir=sqlite_path.parent, delete=False
        )
        backup_path = Path(backup_file.name)
        backup_file.close()
        shutil.copyfile(sqlite_path, backup_path)
    try:
        build_sqlite_fn(sqlite_path, artifacts, edges)
        try:
            write_jsonl_fn(jsonl_path, artifacts)
        except Exception:
            if backup_path is None:
                sqlite_path.unlink(missing_ok=True)
            else:
                _replace_sqlite_with_retry(backup_path, sqlite_path)
                backup_path = None
            raise
    finally:
        if backup_path is not None:
            backup_path.unlink(missing_ok=True)


def build_index(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
    *,
    acquire_lock: bool = True,
) -> tuple[list[Artifact], list[Edge]]:
    if acquire_lock:
        with index_build_lock(output_dir):
            return build_index(root, output_dir, hermes_home, settings, acquire_lock=False)

    _refuse_newer_index(output_dir / "index.sqlite")
    from .scanners import build_edges, collect_artifacts

    artifacts = collect_artifacts(root, hermes_home, settings, okf_root=output_dir / "okfs")
    edges = build_edges(artifacts)
    output_dir.mkdir(parents=True, exist_ok=True)
    _publish_index_generation(
        output_dir / "index.sqlite",
        output_dir / "index.jsonl",
        artifacts,
        edges,
        build_sqlite_fn=build_sqlite,
        write_jsonl_fn=write_jsonl,
    )
    return artifacts, edges


def artifact_type_counts(artifacts: Sequence[Artifact]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for artifact in artifacts:
        counts[artifact.type] = counts.get(artifact.type, 0) + 1
    return dict(sorted(counts.items()))


def _utc_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def index_metadata(db_path: Path) -> dict[str, Any]:
    """Return safe, best-effort metadata for an index database.

    Metadata collection is diagnostic only. It should never prevent the caller
    from attempting the real lookup path.
    """

    metadata: dict[str, Any] = {"index_exists": db_path.exists()}
    if not db_path.exists():
        return metadata

    try:
        stat = db_path.stat()
        metadata.update(
            {
                "index_mtime": _utc_from_timestamp(stat.st_mtime),
                "index_age_seconds": max(0, int(time.time() - stat.st_mtime)),
            }
        )
    except OSError as exc:
        metadata["index_metadata_error"] = f"stat failed: {type(exc).__name__}: {exc}"
        return metadata

    try:
        conn = connect_readonly(db_path)
        try:
            counts = {
                str(row[0]): int(row[1])
                for row in conn.execute("SELECT type, COUNT(*) FROM artifacts GROUP BY type").fetchall()
            }
            artifact_count = sum(counts.values())
            edge_count = int(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
            format_row = conn.execute("PRAGMA user_version").fetchone()
            format_version = int(format_row[0]) if format_row is not None else None
        finally:
            conn.close()
    except Exception as exc:
        metadata["index_metadata_error"] = f"sqlite stats failed: {type(exc).__name__}: {exc}"
    else:
        metadata.update(
            {
                "artifact_count": artifact_count,
                "artifact_counts_by_type": dict(sorted(counts.items())),
                "edge_count": edge_count,
                "index_format_version": format_version,
            }
        )
    return metadata


def readonly_sqlite_uri(path: Path) -> str:
    return f"{path.expanduser().resolve().as_uri()}?mode=ro"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(readonly_sqlite_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def index_format_version(db_path: Path) -> int | None:
    if not db_path.is_file():
        return None
    try:
        conn = connect_readonly(db_path)
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
        finally:
            conn.close()
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
    return "current", version


def index_needs_rebuild(db_path: Path) -> bool:
    state, version = index_format_state(db_path)
    if state == "newer":
        assert version is not None
        raise NewerIndexFormatError(
            expected_version=INDEX_FORMAT_VERSION,
            actual_version=version,
        )
    return state in {"missing", "corrupt", "older"}


def decode_artifact_row(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    output.pop("type_priority", None)
    output.pop("metadata_score", None)
    for field_name in ("triggers_json", "entities_json", "related_json"):
        new_name = field_name.removesuffix("_json")
        try:
            output[new_name] = json.loads(output.pop(field_name))
        except (KeyError, TypeError, json.JSONDecodeError):
            output[new_name] = []
    return output

def get_artifact(db_path: Path, artifact_id: str) -> dict[str, Any] | None:
    conn = connect_readonly(db_path)
    try:
        row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return decode_artifact_row(row) if row else None
    finally:
        conn.close()

def get_neighbors(db_path: Path, artifact_id: str) -> list[dict[str, Any]]:
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute(
            """
            SELECT e.kind, e.evidence, a.*
            FROM edges e
            JOIN artifacts a ON a.id = e.target
            WHERE e.source = ?
            UNION ALL
            SELECT e.kind, e.evidence, a.*
            FROM edges e
            JOIN artifacts a ON a.id = e.source
            WHERE e.target = ?
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
        conn.close()
