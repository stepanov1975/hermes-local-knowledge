"""Persistence for local knowledge JSONL and SQLite indexes."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .models import Artifact, Edge, IndexSettings


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

def build_sqlite(path: Path, artifacts: Sequence[Artifact], edges: Sequence[Edge]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        conn.commit()
        conn.close()
        os.replace(temp_path, path)
    finally:
        try:
            if conn is not None:
                conn.close()
        finally:
            temp_path.unlink(missing_ok=True)

def build_index(
    root: Path,
    output_dir: Path,
    hermes_home: Path,
    settings: IndexSettings | None = None,
) -> tuple[list[Artifact], list[Edge]]:
    from .scanners import build_edges, collect_artifacts

    artifacts = collect_artifacts(root, hermes_home, settings)
    edges = build_edges(artifacts)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "index.jsonl", artifacts)
    build_sqlite(output_dir / "index.sqlite", artifacts, edges)
    return artifacts, edges

def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def decode_artifact_row(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    output.pop("type_priority", None)
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
