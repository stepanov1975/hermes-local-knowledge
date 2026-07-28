#!/usr/bin/env python3
"""Compare frozen local-knowledge indexes and retrieval behavior across git refs.

The first ref is the acceptance baseline.  Every ref is built with its own API
code against private, per-ref clones of one frozen source/runtime/OKF corpus.
Live telemetry is opened read-only only long enough to create a private SQLite
backup; all labels and historical replay cases are then frozen against baseline
artifact IDs.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
EVALUATOR = REPO_ROOT / "scripts" / "evaluate_ref.py"
DEFAULT_API_MODULE = "hermes_local_knowledge.indexer"
POSITIVE_RATINGS = {"useful", "great"}
NEGATIVE_RATINGS = {"not_useful", "noisy", "wrong_artifact", "stale"}
IGNORED_LABEL_VALUES = {"", "none", "null", "xxxx", "sentinel unlikely", "demo"}
JSON_LIST_FIELDS = ("triggers", "entities", "related")
ARTIFACT_FIELDS = (
    "title",
    "path",
    "summary",
    "triggers",
    "entities",
    "related",
    "updated_at",
    "source",
    "search_text",
)
FTS_FIELDS = ("type", "title", "summary", "triggers", "entities", "path", "search_text")
VCS_CACHE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    "htmlcov",
    "build",
    "dist",
    "mutants",
    "node_modules",
}
SCANNER_EXCLUDED_DIR_NAMES = {
    ".git",
    ".archive",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "htmlcov",
    "logs",
    "node_modules",
    "venv",
    ".venv",
    "worktrees",
    ".worktrees",
}
SCANNER_SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".cjs", ".mjs", ".js"}
SKILL_SUPPORT_DIRS = {"references", "templates", "scripts", "assets"}
GENERATED_STATE_DIRS = {"okfs", "okf_index_dirty", "dirty_tokens"}
GENERATED_STATE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".jsonl", ".log", ".lock"}
GENERATED_STATE_NAMES = {"index_build.lock", "okf_index_dirty", "okf_worker.log"}
SCANNER_PATH_KEYS = {
    "source_root",
    "root",
    "state_dir",
    "index_dir",
    "hermes_home",
    "custom_skill_dirs",
    "script_dirs",
    "memory_dirs",
    "runbook_dirs",
}


@dataclass(frozen=True)
class UsageSnapshot:
    before_counts: dict[str, int]
    after_counts: dict[str, int]
    query_only: bool
    backup_path: Path


@dataclass(frozen=True)
class ReplaySearchCase:
    case_id: str
    query: str
    limit: int | None
    artifact_type: str | None
    rebuild_requested: bool


@dataclass(frozen=True)
class ReplayGetCase:
    case_id: str
    artifact_id: str
    rebuild_requested: bool


@dataclass(frozen=True)
class ReplayNeighborCase:
    case_id: str
    artifact_id: str
    limit: int | None
    rebuild_requested: bool


@dataclass(frozen=True)
class RawUsageCorpus:
    positive_rows: tuple[tuple[str, str], ...]
    negative_rows: tuple[tuple[str, str], ...]
    replay_search: tuple[ReplaySearchCase, ...]
    replay_get: tuple[ReplayGetCase, ...]
    replay_neighbors: tuple[ReplayNeighborCase, ...]


@dataclass(frozen=True)
class FrozenUsageCorpus:
    positive_labels: dict[str, set[str]]
    negative_pairs: tuple[tuple[str, str], ...]
    replay_search: tuple[ReplaySearchCase, ...]
    replay_get: tuple[ReplayGetCase, ...]
    replay_neighbors: tuple[ReplayNeighborCase, ...]


@dataclass(frozen=True)
class MetricEvaluation:
    metrics: dict[str, int | float]
    cases: list[dict[str, Any]]


@dataclass(frozen=True)
class IndexOracle:
    valid: bool
    errors: tuple[str, ...]
    artifacts: dict[str, dict[str, Any]]
    fts_rows: dict[str, dict[str, str]]
    fts_hashes: dict[str, str]
    edges: set[tuple[str, str, str, str]]
    summary: dict[str, Any]


@dataclass(frozen=True)
class RefLayout:
    ref: str
    ref_key: str
    checkout: Path
    api_module: str
    source_root: Path
    home: Path
    hermes_home: Path
    state_dir: Path
    synthetic_root: Path
    synthetic_home: Path
    synthetic_hermes_home: Path
    synthetic_state_dir: Path
    settings: dict[str, Any]
    input_manifests: dict[str, dict[str, dict[str, Any]]]


@dataclass
class RefEvaluation:
    ref: str
    ref_key: str
    api_module: str
    oracle: IndexOracle
    positive: MetricEvaluation
    negative: MetricEvaluation
    replay: dict[str, dict[str, Any]]
    synthetic: dict[str, Any]
    evaluator_output: dict[str, Any]
    failures: list[str] = field(default_factory=list)
    accepted: bool = True


@dataclass(frozen=True)
class ComparisonRun:
    accepted: bool
    report: dict[str, Any]
    details: dict[str, Any]


# ---------------------------------------------------------------------------
# Generic private I/O and subprocess helpers


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_json(value: Any) -> str:
    return sha256_text(_canonical_json(value))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_private_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved.chmod(0o700)
    return resolved


def write_private_json(path: Path, payload: Any) -> None:
    ensure_private_directory(path.parent)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, path)
    path.chmod(0o600)


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        rendered = " ".join(command)
        raise RuntimeError(
            f"Command failed ({result.returncode}): {rendered}; stderr_sha256={sha256_text(result.stderr)}"
        )
    return result.stdout


def safe_ref_name(ref: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", ref.strip())
    stem = cleaned.strip("-.") or "ref"
    digest = hashlib.sha1(ref.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}"


def build_child_env(
    checkout: Path,
    *,
    home: Path,
    hermes_home: Path,
    source_root: Path | None,
    state_dir: Path | None,
    explicit_root: bool,
) -> dict[str, str]:
    removed = {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
        "LOCAL_KNOWLEDGE_ROOT",
        "LOCAL_KNOWLEDGE_STATE_DIR",
        "HERMES_HOME",
        "HOME",
    }
    env = {key: value for key, value in os.environ.items() if key not in removed}
    env.update(
        {
            "PYTHONPATH": str(checkout.expanduser().resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONHASHSEED": "0",
            "HOME": str(home.expanduser().resolve()),
            "HERMES_HOME": str(hermes_home.expanduser().resolve()),
        }
    )
    if state_dir is not None:
        env["LOCAL_KNOWLEDGE_STATE_DIR"] = str(state_dir.expanduser().resolve())
    if explicit_root and source_root is not None:
        env["LOCAL_KNOWLEDGE_ROOT"] = str(source_root.expanduser().resolve())
    return env


def _invoke_evaluator(
    checkout: Path,
    request: dict[str, Any],
    request_dir: Path,
    *,
    api_module: str,
    home: Path,
    hermes_home: Path,
    source_root: Path | None = None,
    state_dir: Path | None = None,
    explicit_root: bool = True,
) -> dict[str, Any]:
    ensure_private_directory(request_dir)
    descriptor, raw_path = tempfile.mkstemp(prefix="request-", suffix=".json", dir=request_dir)
    os.close(descriptor)
    request_path = Path(raw_path)
    write_private_json(request_path, request)
    env = build_child_env(
        checkout,
        home=home,
        hermes_home=hermes_home,
        source_root=source_root,
        state_dir=state_dir,
        explicit_root=explicit_root,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(EVALUATOR),
            "--request",
            str(request_path),
            "--ref-root",
            str(checkout.resolve()),
            "--api-module",
            api_module,
        ],
        cwd=checkout,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "ref evaluator did not emit one JSON object "
            f"(stdout_sha256={sha256_text(result.stdout)}, stderr_sha256={sha256_text(result.stderr)})"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ref evaluator did not return a JSON object")
    if result.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(
            "ref evaluator failed: "
            f"{payload.get('error_type', 'unknown')}:{payload.get('error_sha256', sha256_text(result.stderr))}"
        )
    return payload


# ---------------------------------------------------------------------------
# Read-only telemetry snapshot and frozen cases


def sqlite_readonly_uri(path: Path) -> str:
    return f"{path.expanduser().resolve().as_uri()}?mode=ro"


def _readonly_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(sqlite_readonly_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sqlite_table_counts(path: Path, *, connection: sqlite3.Connection | None = None) -> dict[str, int]:
    owns_connection = connection is None
    conn = connection or _readonly_connection(path)
    try:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {
            name: int(conn.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(name)}").fetchone()[0])
            for name in names
        }
    finally:
        if owns_connection:
            conn.close()


def snapshot_usage_database(source: Path, destination: Path) -> UsageSnapshot:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"usage database not found: {source}")
    ensure_private_directory(destination.parent)
    if destination.exists():
        raise FileExistsError(f"private usage snapshot already exists: {destination}")
    source_conn = _readonly_connection(source)
    destination_conn: sqlite3.Connection | None = None
    try:
        query_only = bool(int(source_conn.execute("PRAGMA query_only").fetchone()[0]))
        before = sqlite_table_counts(source, connection=source_conn)
        destination_conn = sqlite3.connect(str(destination))
        source_conn.backup(destination_conn)
        destination_conn.commit()
        after = sqlite_table_counts(source, connection=source_conn)
    finally:
        if destination_conn is not None:
            destination_conn.close()
        source_conn.close()
    destination.chmod(0o600)
    if before != after:
        raise RuntimeError("live usage database row counts changed during read-only backup")
    return UsageSnapshot(before, after, query_only, destination)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quoted_identifier(table)})").fetchall()}


def _column(alias: str, columns: set[str], name: str, default: str = "NULL") -> str:
    return f"{alias}.{_quoted_identifier(name)}" if name in columns else default


def _clean_label_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in IGNORED_LABEL_VALUES else text


def _case_id(kind: str, values: Sequence[Any]) -> str:
    return hash_json([kind, *values])


def read_usage_corpus(path: Path, live_root: Path) -> RawUsageCorpus:
    conn = _readonly_connection(path)
    try:
        event_columns = _table_columns(conn, "usage_events")
        feedback_columns = _table_columns(conn, "feedback")
        if not event_columns or not feedback_columns:
            raise RuntimeError("usage snapshot lacks current usage_events/feedback tables")
        event_root = _column("e", event_columns, "root")
        feedback_root = _column("f", feedback_columns, "root")
        event_query = _column("e", event_columns, "query")
        feedback_query = _column("f", feedback_columns, "query")
        feedback_event = _column("f", feedback_columns, "event_id")
        feedback_artifact = _column("f", feedback_columns, "artifact_id")
        rating = _column("f", feedback_columns, "rating", "''")
        feedback_rows = conn.execute(
            f"""
            SELECT {rating} AS rating,
                   COALESCE(NULLIF({feedback_query}, ''), {event_query}) AS effective_query,
                   {feedback_artifact} AS artifact_id,
                   COALESCE(NULLIF({feedback_root}, ''), {event_root}) AS effective_root
            FROM feedback f
            LEFT JOIN usage_events e ON e.id = {feedback_event}
            """
        ).fetchall()
        root_text = str(live_root.expanduser().resolve())
        positives: set[tuple[str, str]] = set()
        negatives: set[tuple[str, str]] = set()
        for row in feedback_rows:
            if str(row["effective_root"] or "") != root_text:
                continue
            query = _clean_label_text(row["effective_query"])
            artifact_id = _clean_label_text(row["artifact_id"])
            rating_text = _clean_label_text(row["rating"]).lower()
            if not query or not artifact_id:
                continue
            if rating_text in POSITIVE_RATINGS:
                positives.add((query, artifact_id))
            elif rating_text in NEGATIVE_RATINGS:
                negatives.add((query, artifact_id))

        def event_value(name: str, default: str = "NULL") -> str:
            return _column("e", event_columns, name, default)

        event_rows = conn.execute(
            f"""
            SELECT {event_value('tool', "''")} AS tool,
                   {event_value('query')} AS query,
                   {event_value('artifact_id')} AS artifact_id,
                   {event_value('artifact_type')} AS artifact_type,
                   {event_value('limit_value')} AS limit_value,
                   {event_value('rebuild_requested', '0')} AS rebuild_requested,
                   {event_value('success', '0')} AS success,
                   {event_value('root')} AS root
            FROM usage_events e
            """
        ).fetchall()
    finally:
        conn.close()

    search_cases: dict[tuple[str, int | None, str | None], ReplaySearchCase] = {}
    get_cases: dict[str, ReplayGetCase] = {}
    neighbor_cases: dict[tuple[str, int | None], ReplayNeighborCase] = {}
    for row in event_rows:
        if str(row["root"] or "") != root_text or int(row["success"] or 0) != 1:
            continue
        tool = str(row["tool"] or "")
        rebuild = bool(int(row["rebuild_requested"] or 0))
        raw_limit = row["limit_value"]
        limit = None if raw_limit is None else int(raw_limit)
        if tool == "knowledge_search":
            query = str(row["query"] or "")
            if not query:
                continue
            artifact_type = str(row["artifact_type"] or "").strip() or None
            search_key = (query, limit, artifact_type)
            search_previous = search_cases.get(search_key)
            rebuild = rebuild or (search_previous.rebuild_requested if search_previous is not None else False)
            search_cases[search_key] = ReplaySearchCase(
                _case_id("search", search_key), query, limit, artifact_type, rebuild
            )
        elif tool == "knowledge_get":
            artifact_id = str(row["artifact_id"] or "").strip()
            if artifact_id:
                get_previous = get_cases.get(artifact_id)
                rebuild = rebuild or (get_previous.rebuild_requested if get_previous is not None else False)
                get_cases[artifact_id] = ReplayGetCase(
                    _case_id("get", (artifact_id,)), artifact_id, rebuild
                )
        elif tool == "knowledge_neighbors":
            artifact_id = str(row["artifact_id"] or "").strip()
            if artifact_id:
                neighbor_key = (artifact_id, limit)
                neighbor_previous = neighbor_cases.get(neighbor_key)
                rebuild = rebuild or (
                    neighbor_previous.rebuild_requested if neighbor_previous is not None else False
                )
                neighbor_cases[neighbor_key] = ReplayNeighborCase(
                    _case_id("neighbors", neighbor_key), artifact_id, limit, rebuild
                )
    return RawUsageCorpus(
        tuple(sorted(positives)),
        tuple(sorted(negatives)),
        tuple(search_cases[key] for key in sorted(search_cases, key=lambda item: _canonical_json(item))),
        tuple(get_cases[key] for key in sorted(get_cases, key=lambda item: _canonical_json(item))),
        tuple(neighbor_cases[key] for key in sorted(neighbor_cases, key=lambda item: _canonical_json(item))),
    )


def freeze_usage_corpus(raw: RawUsageCorpus, baseline_ids: set[str]) -> FrozenUsageCorpus:
    labels: dict[str, set[str]] = {}
    for query, artifact_id in raw.positive_rows:
        if artifact_id in baseline_ids:
            labels.setdefault(query, set()).add(artifact_id)
    negative_pairs = tuple(
        (query, artifact_id)
        for query, artifact_id in raw.negative_rows
        if artifact_id in baseline_ids
    )
    return FrozenUsageCorpus(
        dict(sorted(labels.items())),
        negative_pairs,
        raw.replay_search,
        raw.replay_get,
        raw.replay_neighbors,
    )


# ---------------------------------------------------------------------------
# Positive/negative rank oracle


def _rank_for_ids(result_ids: Sequence[str], accepted_ids: set[str]) -> int | None:
    for rank, artifact_id in enumerate(result_ids[:10], start=1):
        if artifact_id in accepted_ids:
            return rank
    return None


def _rank_value(rank: int | None) -> float:
    return float("inf") if rank is None else float(rank)


def parent_equivalence_map(artifacts: Mapping[str, Mapping[str, Any]]) -> dict[str, set[str]]:
    equivalents: dict[str, set[str]] = {}
    for artifact_id, artifact in artifacts.items():
        if str(artifact.get("type") or "") != "skill_support_doc":
            continue
        related = artifact.get("related")
        if not isinstance(related, list):
            continue
        parents = {
            str(raw_parent)
            for raw_parent in related
            if str(raw_parent).startswith("skill:")
            and str(raw_parent) in artifacts
            and artifacts[str(raw_parent)].get("type") == "skill"
        }
        if len(parents) != 1:
            continue
        parent = next(iter(parents))
        equivalents.setdefault(artifact_id, set()).add(parent)
        equivalents.setdefault(parent, set()).add(artifact_id)
    return equivalents


def _parent_accepted(accepted: set[str], equivalents: Mapping[str, set[str]]) -> set[str]:
    expanded = set(accepted)
    for artifact_id in accepted:
        expanded.update(equivalents.get(artifact_id, set()))
    return expanded


def compute_positive_evaluation(
    labels: Mapping[str, set[str]],
    results: Mapping[str, Sequence[str]],
    *,
    parent_equivalents: Mapping[str, set[str]],
    errors: set[str] | None = None,
) -> MetricEvaluation:
    exact_counts = {1: 0, 3: 0, 5: 0, 10: 0}
    parent_counts = {1: 0, 3: 0, 5: 0, 10: 0}
    exact_rr = 0.0
    parent_rr = 0.0
    cases: list[dict[str, Any]] = []
    error_queries = errors or set()
    for query, accepted in labels.items():
        result_ids = [str(value) for value in results.get(query, ())][:10]
        exact_rank = _rank_for_ids(result_ids, set(accepted))
        parent_rank = _rank_for_ids(result_ids, _parent_accepted(set(accepted), parent_equivalents))
        for k in exact_counts:
            exact_counts[k] += int(exact_rank is not None and exact_rank <= k)
            parent_counts[k] += int(parent_rank is not None and parent_rank <= k)
        if exact_rank is not None:
            exact_rr += 1.0 / exact_rank
        if parent_rank is not None:
            parent_rr += 1.0 / parent_rank
        cases.append(
            {
                "query_id": sha256_text(query),
                "accepted_count": len(accepted),
                "exact_rank": exact_rank,
                "parent_equiv_rank": parent_rank,
                "search_error": query in error_queries,
            }
        )
    denominator = len(labels)

    def ratio(value: float) -> float:
        return value / denominator if denominator else 0.0

    metrics: dict[str, int | float] = {
        "query_count": denominator,
        "label_count": sum(len(value) for value in labels.values()),
        "hit_at_1": ratio(exact_counts[1]),
        "hit_at_3": ratio(exact_counts[3]),
        "hit_at_5": ratio(exact_counts[5]),
        "hit_at_10": ratio(exact_counts[10]),
        "mrr_at_10": ratio(exact_rr),
        "parent_equiv_hit_at_1": ratio(parent_counts[1]),
        "parent_equiv_hit_at_3": ratio(parent_counts[3]),
        "parent_equiv_hit_at_5": ratio(parent_counts[5]),
        "parent_equiv_hit_at_10": ratio(parent_counts[10]),
        "parent_equiv_mrr_at_10": ratio(parent_rr),
        "search_error_count": len(error_queries),
    }
    return MetricEvaluation(metrics, cases)


def compute_negative_evaluation(
    pairs: Sequence[tuple[str, str]],
    results: Mapping[str, Sequence[str]],
    *,
    errors: set[str] | None = None,
) -> MetricEvaluation:
    counts = {1: 0, 3: 0, 10: 0}
    reciprocal_rank = 0.0
    cases: list[dict[str, Any]] = []
    error_queries = errors or set()
    for query, artifact_id in pairs:
        rank = _rank_for_ids([str(value) for value in results.get(query, ())], {artifact_id})
        for k in counts:
            counts[k] += int(rank is not None and rank <= k)
        if rank is not None:
            reciprocal_rank += 1.0 / rank
        cases.append(
            {
                "pair_id": _case_id("negative", (query, artifact_id)),
                "query_id": sha256_text(query),
                "artifact_id_hash": sha256_text(artifact_id),
                "rank": rank,
                "search_error": query in error_queries,
            }
        )
    denominator = len(pairs)

    def ratio(value: float) -> float:
        return value / denominator if denominator else 0.0

    return MetricEvaluation(
        {
            "pair_count": denominator,
            "bad_hit_at_1": ratio(counts[1]),
            "bad_hit_at_3": ratio(counts[3]),
            "bad_hit_at_10": ratio(counts[10]),
            "negative_mrr_at_10": ratio(reciprocal_rank),
            "search_error_count": len(error_queries),
        },
        cases,
    )


def _compare_metric_evaluations(
    baseline: MetricEvaluation,
    candidate: MetricEvaluation,
    *,
    positive: bool,
) -> dict[str, Any]:
    identity_key = "query_id" if positive else "pair_id"
    rank_fields = ("exact_rank", "parent_equiv_rank") if positive else ("rank",)
    baseline_cases = {str(case[identity_key]): case for case in baseline.cases}
    candidate_cases = {str(case[identity_key]): case for case in candidate.cases}
    regressions: list[dict[str, Any]] = []
    missing = sorted(set(baseline_cases) - set(candidate_cases))
    for case_id, baseline_case in baseline_cases.items():
        candidate_case = candidate_cases.get(case_id)
        if candidate_case is None:
            continue
        for field_name in rank_fields:
            baseline_value = _rank_value(baseline_case.get(field_name))
            candidate_value = _rank_value(candidate_case.get(field_name))
            regressed = candidate_value > baseline_value if positive else candidate_value < baseline_value
            if regressed:
                regressions.append(
                    {
                        identity_key: case_id,
                        "metric": field_name,
                        "baseline_rank": baseline_case.get(field_name),
                        "candidate_rank": candidate_case.get(field_name),
                    }
                )
    aggregate_keys: tuple[str, ...]
    if positive:
        aggregate_keys = (
            "hit_at_1",
            "hit_at_3",
            "hit_at_5",
            "hit_at_10",
            "mrr_at_10",
            "parent_equiv_hit_at_1",
            "parent_equiv_hit_at_3",
            "parent_equiv_hit_at_5",
            "parent_equiv_hit_at_10",
            "parent_equiv_mrr_at_10",
        )
        aggregate_regressions = [
            key
            for key in aggregate_keys
            if float(candidate.metrics[key]) + 1e-12 < float(baseline.metrics[key])
        ]
    else:
        aggregate_keys = ("bad_hit_at_1", "bad_hit_at_3", "bad_hit_at_10", "negative_mrr_at_10")
        aggregate_regressions = [
            key
            for key in aggregate_keys
            if float(candidate.metrics[key]) > float(baseline.metrics[key]) + 1e-12
        ]
    accepted = not regressions and not aggregate_regressions and not missing
    return {
        "accepted": accepted,
        "case_regressions": regressions,
        "aggregate_regressions": aggregate_regressions,
        "missing_case_ids": missing,
    }


# ---------------------------------------------------------------------------
# Structural index oracle


def _decode_json_list(value: Any) -> tuple[list[str], bool]:
    try:
        parsed = json.loads(str(value)) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return [], False
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return [str(item) for item in parsed] if isinstance(parsed, list) else [], False
    return list(parsed), True


def _artifact_list(row: Mapping[str, Any], name: str) -> tuple[list[str], bool]:
    if f"{name}_json" in row:
        return _decode_json_list(row[f"{name}_json"])
    return _decode_json_list(row.get(name, []))


def _empty_or_string(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _oracle_failure(code: str) -> IndexOracle:
    return IndexOracle(
        False,
        (code,),
        {},
        {},
        {},
        set(),
        {
            "valid": False,
            "errors": [code],
            "artifact_count": 0,
            "fts_count": 0,
            "jsonl_count": 0,
            "edge_count": 0,
        },
    )


def inspect_index(state_dir: Path) -> IndexOracle:
    db_path = state_dir / "index.sqlite"
    jsonl_path = state_dir / "index.jsonl"
    if not db_path.is_file() or not jsonl_path.is_file():
        return _oracle_failure("missing_index_output")
    errors: set[str] = set()
    artifacts: dict[str, dict[str, Any]] = {}
    fts_rows: dict[str, dict[str, str]] = {}
    edges: set[tuple[str, str, str, str]] = set()
    artifact_row_count = 0
    fts_row_count = 0
    edge_row_count = 0
    type_counts: dict[str, int] = {}
    try:
        conn = _readonly_connection(db_path)
        try:
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
            if integrity != ["ok"]:
                errors.add("sqlite_integrity")
            artifact_rows = [dict(row) for row in conn.execute("SELECT * FROM artifacts ORDER BY id").fetchall()]
            artifact_row_count = len(artifact_rows)
            raw_fts_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, type, title, summary, triggers, entities, path, search_text FROM artifact_fts ORDER BY id"
                ).fetchall()
            ]
            fts_row_count = len(raw_fts_rows)
            raw_edges = [dict(row) for row in conn.execute("SELECT source, target, kind, evidence FROM edges").fetchall()]
            edge_row_count = len(raw_edges)
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return _oracle_failure("sqlite_read_error")

    for row in raw_fts_rows:
        artifact_id = str(row.get("id") or "")
        if not artifact_id or artifact_id in fts_rows:
            errors.add("duplicate_fts_ids")
            continue
        if not all(isinstance(row.get(name), str) for name in FTS_FIELDS):
            errors.add("invalid_fts_field_types")
        fts_rows[artifact_id] = {name: str(row.get(name) or "") for name in FTS_FIELDS}

    for row in artifact_rows:
        artifact_id = str(row.get("id") or "")
        if not artifact_id or artifact_id in artifacts:
            errors.add("duplicate_artifact_ids")
            continue
        triggers, triggers_ok = _artifact_list(row, "triggers")
        entities, entities_ok = _artifact_list(row, "entities")
        related, related_ok = _artifact_list(row, "related")
        if not (triggers_ok and entities_ok and related_ok):
            errors.add("invalid_artifact_json_fields")
        required_text = ("type", "title", "path", "summary")
        if not all(isinstance(row.get(name), str) for name in required_text):
            errors.add("invalid_artifact_field_types")
        if not _empty_or_string(row.get("updated_at")) or not _empty_or_string(row.get("source")):
            errors.add("invalid_artifact_field_types")
        canonical: dict[str, Any] = {
            "id": artifact_id,
            "type": str(row.get("type") or ""),
            "title": str(row.get("title") or ""),
            "path": str(row.get("path") or ""),
            "summary": str(row.get("summary") or ""),
            "triggers": triggers,
            "entities": entities,
            "related": related,
            "updated_at": None if row.get("updated_at") is None else str(row.get("updated_at")),
            "source": None if row.get("source") is None else str(row.get("source")),
            "search_text": fts_rows.get(artifact_id, {}).get("search_text", ""),
        }
        artifacts[artifact_id] = canonical
        type_name = canonical["type"]
        type_counts[type_name] = type_counts.get(type_name, 0) + 1

    artifact_ids = set(artifacts)
    fts_ids = set(fts_rows)
    if artifact_ids != fts_ids or artifact_row_count != len(artifact_ids) or fts_row_count != len(fts_ids):
        errors.add("artifact_fts_id_coverage")

    for row in raw_edges:
        values = (
            str(row.get("source") or ""),
            str(row.get("target") or ""),
            str(row.get("kind") or ""),
            str(row.get("evidence") or ""),
        )
        if not all(isinstance(row.get(name), str) for name in ("source", "target", "kind", "evidence")):
            errors.add("invalid_edge_field_types")
        edges.add(values)
        if values[0] not in artifact_ids or values[1] not in artifact_ids:
            errors.add("dangling_edges")
    if len(edges) != edge_row_count:
        errors.add("duplicate_edges")

    jsonl_rows: dict[str, dict[str, Any]] = {}
    try:
        with jsonl_path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                row = json.loads(raw_line)
                if not isinstance(row, dict):
                    errors.add("invalid_jsonl_rows")
                    continue
                artifact_id = str(row.get("id") or "")
                if not artifact_id or artifact_id in jsonl_rows:
                    errors.add("duplicate_jsonl_ids")
                    continue
                for name in JSON_LIST_FIELDS:
                    value = row.get(name)
                    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                        errors.add("invalid_jsonl_field_types")
                if not all(isinstance(row.get(name), str) for name in ("id", "type", "title", "path", "summary")):
                    errors.add("invalid_jsonl_field_types")
                jsonl_rows[artifact_id] = row
    except (OSError, json.JSONDecodeError):
        errors.add("jsonl_read_error")
    if set(jsonl_rows) != artifact_ids:
        errors.add("artifact_jsonl_id_coverage")
    for artifact_id in sorted(set(jsonl_rows) & artifact_ids):
        expected = artifacts[artifact_id]
        actual = jsonl_rows[artifact_id]
        fields = ("id", "type", "title", "path", "summary", "triggers", "entities", "related", "updated_at", "source")
        if any(actual.get(name) != expected.get(name) for name in fields):
            errors.add("artifact_jsonl_payload_mismatch")
            break

    fts_hashes = {artifact_id: hash_json(fts_rows[artifact_id]) for artifact_id in sorted(fts_rows)}
    artifact_hash = hash_json({artifact_id: artifacts[artifact_id] for artifact_id in sorted(artifacts)})
    fts_hash = hash_json({artifact_id: fts_hashes[artifact_id] for artifact_id in sorted(fts_hashes)})
    edge_hash = hash_json(sorted(edges))
    jsonl_hash = hash_json({artifact_id: jsonl_rows[artifact_id] for artifact_id in sorted(jsonl_rows)})
    error_list = sorted(errors)
    summary = {
        "valid": not error_list,
        "errors": error_list,
        "artifact_count": len(artifacts),
        "artifact_counts_by_type": dict(sorted(type_counts.items())),
        "fts_count": len(fts_rows),
        "jsonl_count": len(jsonl_rows),
        "edge_count": len(edges),
        "artifact_universe_sha256": hash_json(sorted(artifact_ids)),
        "artifact_payload_sha256": artifact_hash,
        "fts_sha256": fts_hash,
        "jsonl_sha256": jsonl_hash,
        "edges_sha256": edge_hash,
    }
    return IndexOracle(not error_list, tuple(error_list), artifacts, fts_rows, fts_hashes, edges, summary)


def compare_index_oracles(baseline: IndexOracle, candidate: IndexOracle) -> dict[str, Any]:
    baseline_ids = set(baseline.artifacts)
    candidate_ids = set(candidate.artifacts)
    removed = sorted(baseline_ids - candidate_ids)
    added = sorted(candidate_ids - baseline_ids)
    changed_fields: list[dict[str, Any]] = []
    for artifact_id in sorted(baseline_ids & candidate_ids):
        fields = [
            name
            for name in ("type", *ARTIFACT_FIELDS)
            if baseline.artifacts[artifact_id].get(name) != candidate.artifacts[artifact_id].get(name)
        ]
        if fields:
            changed_fields.append({"artifact_id_hash": sha256_text(artifact_id), "fields": fields})
    all_fts_ids = set(baseline.fts_hashes) | set(candidate.fts_hashes)
    changed_fts = sorted(
        artifact_id
        for artifact_id in all_fts_ids
        if baseline.fts_hashes.get(artifact_id) != candidate.fts_hashes.get(artifact_id)
    )
    removed_edges = baseline.edges - candidate.edges
    added_edges = candidate.edges - baseline.edges
    equal = (
        baseline.valid
        and candidate.valid
        and not removed
        and not added
        and not changed_fields
        and not changed_fts
        and not removed_edges
        and not added_edges
    )
    return {
        "equal": equal,
        "baseline_valid": baseline.valid,
        "candidate_valid": candidate.valid,
        "removed_artifact_hashes": [sha256_text(value) for value in removed],
        "added_artifact_hashes": [sha256_text(value) for value in added],
        "changed_artifact_fields": changed_fields,
        "changed_fts_ids": [sha256_text(value) for value in changed_fts],
        "removed_edge_hashes": [hash_json(value) for value in sorted(removed_edges)],
        "added_edge_hashes": [hash_json(value) for value in sorted(added_edges)],
    }


# ---------------------------------------------------------------------------
# Replay and synthetic oracles


def _normalize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_payload(item)
            for key, item in sorted(value.items())
            if key not in {"rank", "type_priority", "metadata_score"}
        }
    if isinstance(value, list):
        return [_normalize_payload(item) for item in value]
    return value


def compare_replay_outputs(
    baseline: Mapping[str, Mapping[str, Mapping[str, Any]]],
    candidate: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    order_changes: list[str] = []
    new_empty: list[str] = []
    new_nonempty: list[str] = []
    candidate_errors: list[str] = []
    get_changes: list[str] = []
    neighbor_changes: list[str] = []
    baseline_errors: list[str] = []

    baseline_search = baseline.get("search", {})
    candidate_search = candidate.get("search", {})
    for case_id, base in baseline_search.items():
        cand = candidate_search.get(case_id)
        if base.get("status") != "ok":
            baseline_errors.append(case_id)
            continue
        if cand is None or cand.get("status") != "ok":
            candidate_errors.append(case_id)
            continue
        base_ids = list(base.get("ids") or [])
        cand_ids = list(cand.get("ids") or [])
        if base_ids and not cand_ids:
            new_empty.append(case_id)
        elif not base_ids and cand_ids:
            new_nonempty.append(case_id)
        elif base_ids != cand_ids:
            order_changes.append(case_id)

    for group_name, changes in (("get", get_changes), ("neighbors", neighbor_changes)):
        baseline_group = baseline.get(group_name, {})
        candidate_group = candidate.get(group_name, {})
        for case_id, base in baseline_group.items():
            cand = candidate_group.get(case_id)
            if base.get("status") != "ok":
                baseline_errors.append(case_id)
                continue
            if cand is None or cand.get("status") != "ok":
                candidate_errors.append(case_id)
                continue
            if _normalize_payload(base.get("payload")) != _normalize_payload(cand.get("payload")):
                changes.append(case_id)

    accepted = not (
        order_changes
        or new_empty
        or new_nonempty
        or candidate_errors
        or get_changes
        or neighbor_changes
        or baseline_errors
    )
    return {
        "accepted": accepted,
        "order_changes": sorted(order_changes),
        "new_empty": sorted(new_empty),
        "new_nonempty": sorted(new_nonempty),
        "candidate_errors": sorted(candidate_errors),
        "baseline_errors": sorted(baseline_errors),
        "get_changes": sorted(get_changes),
        "neighbor_changes": sorted(neighbor_changes),
    }


def _expected_before_pairs(value: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for before, after_values in value.items():
            values = after_values if isinstance(after_values, list) else [after_values]
            pairs.extend((str(before), str(after)) for after in values)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, list) and len(item) == 2:
                pairs.append((str(item[0]), str(item[1])))
            elif isinstance(item, dict) and "before" in item and "after" in item:
                pairs.append((str(item["before"]), str(item["after"])))
    return pairs


def evaluate_synthetic_cases(cases: Sequence[Mapping[str, Any]], outcomes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    passed = 0
    for case in cases:
        case_id = str(case["case_id"])
        outcome = outcomes.get(case_id, {})
        ids = [str(value) for value in outcome.get("ids", [])] if outcome.get("status") == "ok" else []
        reasons: list[str] = []
        if outcome.get("status") != "ok":
            reasons.append("search_error")
        for expected in case.get("expected_top", []):
            if ids[:1] != [str(expected)]:
                reasons.append("expected_top")
        for expected in case.get("expected_anywhere", []):
            if str(expected) not in ids:
                reasons.append("expected_anywhere")
        for before, after in _expected_before_pairs(case.get("expected_before")):
            if before not in ids or after not in ids or ids.index(before) >= ids.index(after):
                reasons.append("expected_before")
        if reasons:
            failures.append({"case_id": case_id, "reasons": sorted(set(reasons))})
        else:
            passed += 1
    return {
        "case_count": len(cases),
        "passed": passed,
        "failed": len(failures),
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Frozen filesystem topology


def _skip_entry(path: Path, root: Path, state_root: Path | None, *, is_dir: bool) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    if any(part in VCS_CACHE_DIRS or part.endswith(".egg-info") for part in rel.parts):
        return True
    if state_root is not None:
        try:
            state_rel = path.relative_to(state_root)
        except ValueError:
            state_rel = None
        if state_rel is not None:
            if state_rel.parts and state_rel.parts[0] in GENERATED_STATE_DIRS:
                return True
            if not is_dir and (path.suffix.lower() in GENERATED_STATE_SUFFIXES or path.name in GENERATED_STATE_NAMES):
                return True
    return False


def _file_manifest_record(path: Path, label: str) -> dict[str, Any]:
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise RuntimeError(f"scanner input changed while hashing: {label}")
    return {
        "kind": "file",
        "size": len(payload),
        "mode": stat.S_IMODE(after.st_mode),
        "sha256": _sha256_bytes(payload),
    }


def _setting_list(settings: Mapping[str, Any] | None, key: str, default: Sequence[str]) -> list[str]:
    value = (settings or {}).get(key, default)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _scanner_roots(root: Path, values: Sequence[str]) -> set[Path]:
    output: set[Path] = set()
    for value in values:
        path = Path(value).expanduser()
        if path.is_absolute():
            try:
                path = path.resolve(strict=False).relative_to(root)
            except ValueError:
                continue
        if ".." in path.parts:
            continue
        output.add(path)
    return output


def _path_under(path: Path, roots: set[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _scanner_skip(
    path: Path,
    root: Path,
    state_root: Path | None,
    excluded_names: set[str],
    *,
    is_dir: bool,
) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    if any(part in excluded_names for part in rel.parts):
        return True
    if state_root is None:
        return False
    try:
        state_rel = path.relative_to(state_root)
    except ValueError:
        return False
    if state_rel.parts and state_rel.parts[0] in GENERATED_STATE_DIRS:
        return True
    return not is_dir and (path.suffix.lower() in GENERATED_STATE_SUFFIXES or path.name in GENERATED_STATE_NAMES)


def scanner_tree_inventory(
    root: Path,
    *,
    state_root: Path | None = None,
    settings: Mapping[str, Any] | None = None,
    runtime_skills: bool = False,
) -> dict[str, tuple[str, Path]]:
    """Return scanner-readable files plus name-only skill support inputs."""

    root = root.expanduser().resolve()
    if not root.exists():
        return {}
    excluded_names = SCANNER_EXCLUDED_DIR_NAMES | set(_setting_list(settings, "exclude_dir_names", ()))
    if runtime_skills:
        skill_roots = {Path(".")}
        script_roots: set[Path] = set()
        include_markdown = False
    else:
        skill_roots = _scanner_roots(root, _setting_list(settings, "custom_skill_dirs", ("custom_skills",)))
        script_roots = _scanner_roots(root, _setting_list(settings, "script_dirs", ("scripts",)))
        include_markdown = bool((settings or {}).get("include_markdown_docs", True))

    files: dict[Path, Path] = {}
    directory_links: dict[Path, Path] = {}

    def walk(directory: Path) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            path = Path(entry.path)
            is_dir = entry.is_dir(follow_symlinks=False)
            if _scanner_skip(path, root, state_root, excluded_names, is_dir=is_dir):
                continue
            rel = path.relative_to(root)
            if entry.is_symlink() and entry.is_dir(follow_symlinks=True):
                directory_links[rel] = path
            elif is_dir:
                walk(path)
            elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                files[rel] = path

    walk(root)

    changed = True
    while changed:
        changed = False
        for rel, path in directory_links.items():
            for roots in (skill_roots, script_roots):
                if not _path_under(rel, roots):
                    continue
                try:
                    target_rel = path.resolve(strict=True).relative_to(root)
                except (OSError, RuntimeError, ValueError):
                    continue
                if target_rel not in roots:
                    roots.add(target_rel)
                    changed = True

    skill_dirs = {
        rel.parent
        for rel in files
        if rel.name == "SKILL.md" and _path_under(rel, skill_roots)
    }
    inventory: dict[str, tuple[str, Path]] = {}
    for rel, path in directory_links.items():
        if _path_under(rel, skill_roots) or _path_under(rel, script_roots):
            inventory[rel.as_posix()] = ("symlink", path)
    for rel, path in files.items():
        support_input = False
        for skill_dir in skill_dirs:
            try:
                skill_rel = rel.relative_to(skill_dir)
            except ValueError:
                continue
            if len(skill_rel.parts) >= 2 and skill_rel.parts[0] in SKILL_SUPPORT_DIRS:
                support_input = True
                break
        readable = (
            (rel.name == "SKILL.md" and _path_under(rel, skill_roots))
            or (include_markdown and rel.suffix == ".md")
            or (runtime_skills and support_input and rel.suffix == ".md")
            or (rel.suffix in SCANNER_SCRIPT_SUFFIXES and _path_under(rel, script_roots))
        )
        if not readable and not support_input:
            continue
        kind = "symlink" if path.is_symlink() else "readable" if readable else "name_only"
        inventory[rel.as_posix()] = (kind, path)
    return dict(sorted(inventory.items()))


def scanner_tree_manifest(
    root: Path,
    *,
    state_root: Path | None = None,
    settings: Mapping[str, Any] | None = None,
    runtime_skills: bool = False,
) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for rel, (kind, path) in scanner_tree_inventory(
        root,
        state_root=state_root,
        settings=settings,
        runtime_skills=runtime_skills,
    ).items():
        if kind == "symlink":
            manifest[rel] = {
                "kind": "symlink",
                "target": os.readlink(path),
                "resolved": str(path.resolve(strict=False)),
            }
        elif kind == "name_only":
            manifest[rel] = {"kind": "name_only", "mode": stat.S_IMODE(path.stat().st_mode)}
        else:
            manifest[rel] = _file_manifest_record(path, rel)
    return manifest


def tree_manifest(root: Path, *, state_root: Path | None = None) -> dict[str, dict[str, Any]]:
    root = root.expanduser().resolve()
    if not root.exists():
        return {}
    manifest: dict[str, dict[str, Any]] = {}

    def walk(directory: Path) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            path = Path(entry.path)
            is_symlink = entry.is_symlink()
            is_dir = entry.is_dir(follow_symlinks=False)
            if _skip_entry(path, root, state_root, is_dir=is_dir):
                continue
            rel = path.relative_to(root).as_posix()
            if is_symlink:
                manifest[rel] = {
                    "kind": "symlink",
                    "target": os.readlink(path),
                    "resolved": str(path.resolve(strict=False)),
                }
            elif is_dir:
                walk(path)
            elif entry.is_file(follow_symlinks=False):
                manifest[rel] = _file_manifest_record(path, rel)

    walk(root)
    return dict(sorted(manifest.items()))


def runtime_manifest(
    hermes_home: Path,
    *,
    state_root: Path | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    hermes_home = hermes_home.expanduser().resolve()
    output: dict[str, dict[str, Any]] = {}
    for rel in (Path("config.yaml"), Path("cron/jobs.json"), Path("skills")):
        path = hermes_home / rel
        if rel != Path("skills") and path.is_file():
            output[rel.as_posix()] = _file_manifest_record(path, rel.as_posix())
        elif path.is_symlink():
            output[rel.as_posix()] = {
                "kind": "symlink",
                "target": os.readlink(path),
                "resolved": str(path.resolve(strict=False)),
            }
        elif path.is_file():
            output[rel.as_posix()] = _file_manifest_record(path, rel.as_posix())
        elif path.is_dir():
            for child_rel, record in scanner_tree_manifest(
                path,
                settings=settings,
                runtime_skills=rel == Path("skills"),
            ).items():
                output[(rel / child_rel).as_posix()] = record
    return dict(sorted(output.items()))


def okf_manifest(tools_dir: Path) -> dict[str, dict[str, Any]]:
    if not tools_dir.exists():
        return {}
    return {
        path.name: _file_manifest_record(path, path.name)
        for path in sorted(tools_dir.glob("*.md"))
        if path.is_file()
    }


def assert_manifest_unchanged(name: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    if before != after:
        raise RuntimeError(f"{name} scanner inputs changed")


def _copy_scanner_tree(
    source: Path,
    destination: Path,
    *,
    state_root: Path | None = None,
    settings: Mapping[str, Any] | None = None,
    runtime_skills: bool = False,
) -> None:
    source = source.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"snapshot destination already exists: {destination}")
    destination.mkdir(parents=True, mode=0o700)
    for rel, (kind, source_path) in scanner_tree_inventory(
        source,
        state_root=state_root,
        settings=settings,
        runtime_skills=runtime_skills,
    ).items():
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if kind == "symlink":
            target.symlink_to(
                os.readlink(source_path),
                target_is_directory=source_path.is_dir(),
            )
        elif kind == "name_only":
            target.touch()
            target.chmod(stat.S_IMODE(source_path.stat().st_mode))
        else:
            shutil.copy2(source_path, target, follow_symlinks=False)


def copy_scanner_snapshot(
    source: Path,
    destination: Path,
    *,
    state_root: Path,
    settings: Mapping[str, Any] | None = None,
) -> None:
    _copy_scanner_tree(
        source,
        destination,
        state_root=state_root.expanduser().resolve(),
        settings=settings,
    )


def _materialize_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, destination, follow_symlinks=True)


def _assert_runtime_skill_symlinks_are_copied(hermes_home: Path) -> None:
    skills = hermes_home / "skills"
    if not (skills.exists() or skills.is_symlink()):
        return
    links = [skills] if skills.is_symlink() else list(_symlink_paths(skills))
    copied_root = None if skills.is_symlink() else skills.resolve()
    for link in links:
        try:
            target = link.resolve(strict=True)
        except FileNotFoundError:
            continue
        except RuntimeError as exc:
            raise RuntimeError("runtime skill symlink target cannot be resolved") from exc
        if not _is_within(target, hermes_home):
            continue
        if copied_root is not None and _is_within(target, copied_root):
            continue
        raise RuntimeError("runtime skill symlink target is outside the copied Hermes skills subtree")


def copy_runtime_snapshot(
    hermes_home: Path,
    destination: Path,
    *,
    settings: Mapping[str, Any] | None = None,
) -> None:
    if destination.exists():
        raise FileExistsError(f"runtime snapshot destination already exists: {destination}")
    live_home = hermes_home.expanduser().resolve()
    _assert_runtime_skill_symlinks_are_copied(live_home)
    destination.mkdir(parents=True, mode=0o700)
    for rel in (Path("config.yaml"), Path("cron/jobs.json")):
        _materialize_file(live_home / rel, destination / rel)
    skills = live_home / "skills"
    if skills.is_symlink():
        (destination / "skills").symlink_to(os.readlink(skills), target_is_directory=skills.is_dir())
    elif skills.is_dir():
        _copy_scanner_tree(
            skills,
            destination / "skills",
            settings=settings,
            runtime_skills=True,
        )
    config = destination / "config.yaml"
    if config.exists():
        config.chmod(0o600)


def clone_snapshot(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"ref snapshot clone already exists: {destination}")
    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)


def _map_path(path: Path, mappings: Sequence[tuple[Path, Path]]) -> Path | None:
    resolved = path.expanduser().resolve(strict=False)
    for live, clone in sorted(mappings, key=lambda pair: len(pair[0].parts), reverse=True):
        live_resolved = live.expanduser().resolve(strict=False)
        try:
            rel = resolved.relative_to(live_resolved)
        except ValueError:
            continue
        return clone.expanduser().resolve(strict=False) / rel
    return None


def _symlink_paths(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        for name in list(dirnames):
            path = directory / name
            if path.is_symlink():
                yield path
                dirnames.remove(name)
        for name in filenames:
            path = directory / name
            if path.is_symlink():
                yield path


def rewrite_clone_symlinks(
    source_clone: Path,
    runtime_clone: Path,
    *,
    live_source: Path,
    live_hermes_home: Path,
    clone_source: Path,
    clone_hermes_home: Path,
) -> None:
    mappings = (
        (live_source.resolve(), clone_source.resolve()),
        (live_hermes_home.resolve(), clone_hermes_home.resolve()),
    )
    for clone_root, live_root in ((source_clone, live_source), (runtime_clone, live_hermes_home)):
        for link in list(_symlink_paths(clone_root)):
            relative_link = link.relative_to(clone_root)
            raw_target = os.readlink(link)
            if os.path.isabs(raw_target) or ntpath.isabs(raw_target):
                original_target = link.resolve(strict=False)
            else:
                original_target = (live_root / relative_link).parent / raw_target
            mapped = _map_path(original_target, mappings)
            if mapped is None:
                continue
            is_directory = mapped.is_dir() if mapped.exists() else link.is_dir()
            link.unlink()
            link.symlink_to(mapped, target_is_directory=is_directory)


def rewrite_local_knowledge_config(path: Path, mappings: Sequence[tuple[Path, Path]]) -> None:
    if not path.is_file():
        return
    replacements = {str(live): str(clone) for live, clone in mappings}
    replacement_pattern = (
        re.compile("|".join(re.escape(value) for value in sorted(replacements, key=len, reverse=True)))
        if replacements
        else None
    )
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_section = False
    active_path_list = False
    output: list[str] = []
    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent == 0 and stripped.startswith("local_knowledge:"):
            in_section = True
            active_path_list = False
            output.append(line)
            continue
        if in_section and stripped.strip() and indent == 0 and not stripped.startswith("#"):
            in_section = False
            active_path_list = False
        rewrite = False
        if in_section:
            key_match = re.match(r"^\s{2}([A-Za-z0-9_]+):", line)
            if key_match:
                active_path_list = key_match.group(1) in SCANNER_PATH_KEYS
                rewrite = active_path_list
            elif active_path_list and indent > 2:
                rewrite = True
            elif indent <= 2 and stripped.strip():
                active_path_list = False
        if rewrite and replacement_pattern is not None:
            line = replacement_pattern.sub(lambda match: replacements[match.group(0)], line)
        output.append(line)
    path.write_text("".join(output), encoding="utf-8")
    path.chmod(0o600)


def _rewrite_settings(settings: Mapping[str, Any], mappings: Sequence[tuple[Path, Path]]) -> dict[str, Any]:
    output = dict(settings)
    for key in ("custom_skill_dirs", "script_dirs", "memory_dirs", "runbook_dirs"):
        raw_values = output.get(key, [])
        values = list(raw_values) if isinstance(raw_values, (list, tuple)) else [raw_values]
        rewritten: list[str] = []
        for value in values:
            text = str(value)
            path = Path(text)
            if path.is_absolute():
                mapped = _map_path(path, mappings)
                text = str(mapped) if mapped is not None else text
            rewritten.append(text)
        output[key] = rewritten
    if isinstance(output.get("known_entities"), tuple):
        output["known_entities"] = list(output["known_entities"])
    if isinstance(output.get("exclude_dir_names"), tuple):
        output["exclude_dir_names"] = list(output["exclude_dir_names"])
    return output


def _scanner_root(source_root: Path, value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else source_root / path


def assert_isolated_topology(
    source_root: Path,
    hermes_home: Path,
    state_dir: Path,
    settings: Mapping[str, Any],
    *,
    live_roots: Sequence[Path],
) -> None:
    scanner_roots = [source_root, hermes_home / "skills", state_dir / "okfs"]
    for key in ("custom_skill_dirs", "script_dirs", "memory_dirs", "runbook_dirs"):
        raw = settings.get(key, [])
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        scanner_roots.extend(_scanner_root(source_root, value) for value in values)
    live = tuple(path.expanduser().resolve(strict=False) for path in live_roots)
    clone_roots = tuple(
        path.expanduser().resolve(strict=False)
        for path in (source_root, hermes_home, state_dir)
    )

    def points_to_live(path: Path) -> bool:
        resolved = path.expanduser().resolve(strict=False)
        if any(_is_within(resolved, clone_root) for clone_root in clone_roots):
            return False
        return any(_is_within(resolved, live_root) for live_root in live)

    for scanner_root in scanner_roots:
        if points_to_live(scanner_root):
            raise RuntimeError("configured scanner root still points to a live tree")
    for clone_root in (source_root, hermes_home):
        for link in _symlink_paths(clone_root):
            if points_to_live(link):
                raise RuntimeError("scanner-followable symlink still points to a live tree")


def _copy_okfs(source_tools: Path, destination_tools: Path) -> None:
    ensure_private_directory(destination_tools)
    if not source_tools.exists():
        return
    for path in sorted(source_tools.glob("*.md")):
        if path.is_file():
            destination = destination_tools / path.name
            shutil.copy2(path, destination, follow_symlinks=True)
            destination.chmod(0o600)


def _state_for_clone(
    live_state: Path,
    live_source: Path,
    live_hermes: Path,
    clone_source: Path,
    clone_hermes: Path,
    ref_dir: Path,
) -> Path:
    mapped = _map_path(live_state, ((live_source, clone_source), (live_hermes, clone_hermes)))
    return mapped if mapped is not None else ref_dir / "state"


# ---------------------------------------------------------------------------
# Cross-ref orchestration


def prepare_worktree(ref: str, base_dir: Path, created: list[Path], ref_key: str) -> Path:
    if ref in {"WORKTREE", "."}:
        return REPO_ROOT
    worktree = base_dir / "worktrees" / ref_key
    if worktree.exists():
        raise FileExistsError(f"ref worktree path already exists: {worktree}")
    worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run(["git", "worktree", "add", "--detach", str(worktree), ref], cwd=REPO_ROOT)
    created.append(worktree)
    return worktree


def cleanup_worktrees(created_worktrees: list[Path], *, keep: bool) -> None:
    if keep:
        return
    errors: list[str] = []
    for worktree in reversed(created_worktrees):
        try:
            run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT)
        except Exception as exc:
            errors.append(str(exc))
    try:
        run(["git", "worktree", "prune"], cwd=REPO_ROOT)
    except Exception as exc:
        errors.append(str(exc))
    for error in errors:
        print(f"WARNING: worktree cleanup failed: {error}", file=sys.stderr)


def _resolve_baseline_config(
    checkout: Path,
    args: argparse.Namespace,
    base_dir: Path,
) -> dict[str, Any]:
    discovery_home = ensure_private_directory(base_dir / "discovery-home")
    hermes_home = args.hermes_home.expanduser().resolve()
    return _invoke_evaluator(
        checkout,
        {
            "action": "resolve_config",
            "hermes_home": str(hermes_home),
            "root_override": None if args.root is None else str(args.root.expanduser().resolve()),
        },
        base_dir / "requests",
        api_module=DEFAULT_API_MODULE,
        home=discovery_home,
        hermes_home=hermes_home,
        explicit_root=False,
    )


def _freeze_inputs(
    live_source: Path,
    live_hermes: Path,
    live_state: Path,
    base_dir: Path,
    settings: Mapping[str, Any],
) -> tuple[dict[str, Path], dict[str, Any]]:
    frozen_dir = ensure_private_directory(base_dir / "frozen")
    source_before = scanner_tree_manifest(live_source, state_root=live_state, settings=settings)
    runtime_before = runtime_manifest(live_hermes, state_root=live_state, settings=settings)
    okf_before = okf_manifest(live_state / "okfs" / "tools")

    source_snapshot = frozen_dir / "source"
    runtime_snapshot = frozen_dir / "runtime"
    okf_snapshot = frozen_dir / "okfs" / "tools"
    copy_scanner_snapshot(live_source, source_snapshot, state_root=live_state, settings=settings)
    copy_runtime_snapshot(live_hermes, runtime_snapshot, settings=settings)
    _copy_okfs(live_state / "okfs" / "tools", okf_snapshot)

    source_after = scanner_tree_manifest(live_source, state_root=live_state, settings=settings)
    runtime_after = runtime_manifest(live_hermes, state_root=live_state, settings=settings)
    okf_after = okf_manifest(live_state / "okfs" / "tools")
    assert_manifest_unchanged("live source", source_before, source_after)
    assert_manifest_unchanged("live runtime", runtime_before, runtime_after)
    assert_manifest_unchanged("live OKF", okf_before, okf_after)

    frozen_source = scanner_tree_manifest(
        source_snapshot,
        state_root=_map_path(live_state, ((live_source, source_snapshot),)),
        settings=settings,
    )
    frozen_runtime = runtime_manifest(runtime_snapshot, settings=settings)
    frozen_okf = okf_manifest(okf_snapshot)
    manifests = {
        "live_source_sha256": hash_json(source_before),
        "live_runtime_sha256": hash_json(runtime_before),
        "live_okf_sha256": hash_json(okf_before),
        "frozen_source_sha256": hash_json(frozen_source),
        "frozen_runtime_sha256": hash_json(frozen_runtime),
        "frozen_okf_sha256": hash_json(frozen_okf),
        "frozen_source_files": sum(1 for value in frozen_source.values() if value["kind"] == "file"),
        "frozen_runtime_files": sum(1 for value in frozen_runtime.values() if value["kind"] == "file"),
        "frozen_okf_files": len(frozen_okf),
    }
    write_private_json(frozen_dir / "manifests.json", manifests)
    return {
        "source": source_snapshot,
        "runtime": runtime_snapshot,
        "okfs": okf_snapshot,
    }, manifests


def _materialize_synthetic(
    baseline_checkout: Path,
    base_dir: Path,
    baseline_home: Path,
    baseline_hermes: Path,
) -> tuple[dict[str, Path], list[dict[str, Any]], dict[str, Any]]:
    frozen_dir = ensure_private_directory(base_dir / "frozen")
    destination = frozen_dir / "synthetic"
    fixture_file = baseline_checkout / "scripts" / "evaluation_fixture.py"
    if not fixture_file.is_file():
        # Pinned pre-rewrite refs kept the same deterministic builder in this test module.
        fixture_file = baseline_checkout / "tests" / "test_indexer.py"
    output = _invoke_evaluator(
        baseline_checkout,
        {
            "action": "materialize_fixture",
            "test_file": str(fixture_file),
            "destination": str(destination),
        },
        base_dir / "requests",
        api_module=DEFAULT_API_MODULE,
        home=baseline_home,
        hermes_home=baseline_hermes,
        explicit_root=False,
    )
    source = Path(str(output["source_root"]))
    hermes = Path(str(output["hermes_home"]))
    regression_path = baseline_checkout / "tests" / "search_regression_cases.json"
    regression_payload = json.loads(regression_path.read_text(encoding="utf-8"))
    if not isinstance(regression_payload, list):
        raise RuntimeError("pinned search regression file is not a list")
    regression = [dict(value) for value in regression_payload if isinstance(value, dict)]
    write_private_json(frozen_dir / "search_regression_cases.json", regression)
    manifests = {
        "source_sha256": hash_json(tree_manifest(source)),
        "runtime_sha256": hash_json(tree_manifest(hermes)),
        "cases_sha256": hash_json(regression),
    }
    return {"source": source, "runtime": hermes}, regression, manifests


def _validate_ref_config(
    resolved: Mapping[str, Any],
    *,
    source_root: Path,
    state_dir: Path,
    hermes_home: Path,
    settings: Mapping[str, Any],
    baseline_config: Mapping[str, Any],
) -> dict[str, Any]:
    mismatches: list[str] = []
    expected_paths = {
        "source_root": source_root.resolve(),
        "state_dir": state_dir.resolve(),
        "hermes_home": hermes_home.resolve(),
    }
    for key, expected in expected_paths.items():
        actual = Path(str(resolved.get(key) or "")).expanduser().resolve(strict=False)
        if actual != expected:
            mismatches.append(key)
    raw_settings = resolved.get("settings")
    resolved_settings = dict(raw_settings) if isinstance(raw_settings, Mapping) else {}
    if not isinstance(raw_settings, Mapping) or resolved_settings != dict(settings):
        mismatches.append("settings")
    for key in ("source_root_source", "state_dir_source", "include_markdown_docs_source"):
        if resolved.get(key) != baseline_config.get(key):
            mismatches.append(key)
    if mismatches:
        raise RuntimeError(f"ref configuration contract mismatch: {','.join(sorted(set(mismatches)))}")
    return resolved_settings


def _create_ref_layout(
    ref: str,
    ref_key: str,
    checkout: Path,
    api_module: str,
    base_dir: Path,
    frozen: Mapping[str, Path],
    synthetic: Mapping[str, Path],
    baseline_config: Mapping[str, Any],
    live_source: Path,
    live_hermes: Path,
    live_state: Path,
    *,
    root_was_explicit: bool,
) -> RefLayout:
    ref_dir = ensure_private_directory(base_dir / "refs" / ref_key)
    source_root = ref_dir / "source"
    home = ensure_private_directory(ref_dir / "home")
    hermes_home = home / ".hermes"
    clone_snapshot(frozen["source"], source_root)
    clone_snapshot(frozen["runtime"], hermes_home)
    state_dir = _state_for_clone(
        live_state,
        live_source,
        live_hermes,
        source_root,
        hermes_home,
        ref_dir,
    )
    mappings = (
        (live_state, state_dir),
        (live_source, source_root),
        (live_hermes, hermes_home),
    )
    rewrite_clone_symlinks(
        source_root,
        hermes_home,
        live_source=live_source,
        live_hermes_home=live_hermes,
        clone_source=source_root,
        clone_hermes_home=hermes_home,
    )
    rewrite_local_knowledge_config(hermes_home / "config.yaml", mappings)
    raw_baseline_settings = baseline_config.get("settings")
    if not isinstance(raw_baseline_settings, Mapping):
        raise RuntimeError("baseline configuration has invalid scanner settings")
    expected_settings = _rewrite_settings(raw_baseline_settings, mappings)
    ensure_private_directory(state_dir)
    _copy_okfs(frozen["okfs"], state_dir / "okfs" / "tools")
    resolved_ref = _invoke_evaluator(
        checkout,
        {
            "action": "resolve_config",
            "hermes_home": str(hermes_home),
            "root_override": str(source_root) if root_was_explicit else None,
        },
        base_dir / "requests",
        api_module=api_module,
        home=home,
        hermes_home=hermes_home,
        explicit_root=False,
    )
    ref_settings = _validate_ref_config(
        resolved_ref,
        source_root=source_root,
        state_dir=state_dir,
        hermes_home=hermes_home,
        settings=expected_settings,
        baseline_config=baseline_config,
    )
    assert_isolated_topology(
        source_root,
        hermes_home,
        state_dir,
        ref_settings,
        live_roots=(live_source, live_hermes),
    )

    synthetic_root = ref_dir / "synthetic" / "source"
    synthetic_home = ensure_private_directory(ref_dir / "synthetic" / "home")
    synthetic_hermes = synthetic_home / ".hermes"
    clone_snapshot(synthetic["source"], synthetic_root)
    clone_snapshot(synthetic["runtime"], synthetic_hermes)
    rewrite_clone_symlinks(
        synthetic_root,
        synthetic_hermes,
        live_source=synthetic["source"],
        live_hermes_home=synthetic["runtime"],
        clone_source=synthetic_root,
        clone_hermes_home=synthetic_hermes,
    )
    synthetic_state = ensure_private_directory(ref_dir / "synthetic" / "state")
    default_settings = {
        "custom_skill_dirs": ["custom_skills"],
        "script_dirs": ["scripts", "hermes_home/scripts"],
        "memory_dirs": ["memory"],
        "runbook_dirs": ["docs"],
    }
    assert_isolated_topology(
        synthetic_root,
        synthetic_hermes,
        synthetic_state,
        default_settings,
        live_roots=(synthetic["source"], synthetic["runtime"], live_source, live_hermes),
    )

    source_state = state_dir if _is_within(state_dir.resolve(), source_root.resolve()) else None
    input_manifests = {
        "source": tree_manifest(source_root, state_root=source_state),
        "runtime": runtime_manifest(hermes_home, state_root=state_dir),
        "okfs": okf_manifest(state_dir / "okfs" / "tools"),
        "synthetic_source": tree_manifest(synthetic_root),
        "synthetic_runtime": tree_manifest(synthetic_hermes),
    }
    return RefLayout(
        ref,
        ref_key,
        checkout,
        api_module,
        source_root,
        home,
        hermes_home,
        state_dir,
        synthetic_root,
        synthetic_home,
        synthetic_hermes,
        synthetic_state,
        ref_settings,
        input_manifests,
    )


def _assert_ref_inputs_unchanged(layout: RefLayout) -> None:
    source_state = layout.state_dir if _is_within(layout.state_dir.resolve(), layout.source_root.resolve()) else None
    after = {
        "source": tree_manifest(layout.source_root, state_root=source_state),
        "runtime": runtime_manifest(layout.hermes_home, state_root=layout.state_dir),
        "okfs": okf_manifest(layout.state_dir / "okfs" / "tools"),
        "synthetic_source": tree_manifest(layout.synthetic_root),
        "synthetic_runtime": tree_manifest(layout.synthetic_hermes_home),
    }
    for name, before in layout.input_manifests.items():
        assert_manifest_unchanged(f"{layout.ref_key} {name}", before, after[name])


def _build_ref(layout: RefLayout, request_dir: Path) -> dict[str, Any]:
    request = {
        "action": "build",
        "builds": [
            {
                "name": "full",
                "root": str(layout.source_root),
                "state_dir": str(layout.state_dir),
                "hermes_home": str(layout.hermes_home),
                "home": str(layout.home),
                "settings": layout.settings,
            },
            {
                "name": "synthetic",
                "root": str(layout.synthetic_root),
                "state_dir": str(layout.synthetic_state_dir),
                "hermes_home": str(layout.synthetic_hermes_home),
                "home": str(layout.synthetic_home),
                "settings": None,
            },
        ],
    }
    output = _invoke_evaluator(
        layout.checkout,
        request,
        request_dir,
        api_module=layout.api_module,
        home=layout.home,
        hermes_home=layout.hermes_home,
        source_root=layout.source_root,
        state_dir=layout.state_dir,
    )
    _assert_ref_inputs_unchanged(layout)
    return output


def _case_file_payload(frozen: FrozenUsageCorpus, regression: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    positive = [
        {
            "query_id": sha256_text(query),
            "query": query,
            "accepted_ids": sorted(accepted),
        }
        for query, accepted in frozen.positive_labels.items()
    ]
    negative = [
        {
            "pair_id": _case_id("negative", (query, artifact_id)),
            "query_id": sha256_text(query),
            "query": query,
            "artifact_id": artifact_id,
        }
        for query, artifact_id in frozen.negative_pairs
    ]
    replay = {
        "search": [asdict(case) for case in frozen.replay_search],
        "get": [asdict(case) for case in frozen.replay_get],
        "neighbors": [asdict(case) for case in frozen.replay_neighbors],
    }
    synthetic_cases: list[dict[str, Any]] = []
    for case in regression:
        copied = dict(case)
        copied["case_id"] = sha256_text(str(case.get("name") or case.get("query") or "case"))
        synthetic_cases.append(copied)
    return {
        "labels": {"positive": positive, "negative": negative},
        "replay": replay,
        "synthetic": synthetic_cases,
    }


def _evaluate_ref(layout: RefLayout, case_file: Path, request_dir: Path) -> dict[str, Any]:
    return _invoke_evaluator(
        layout.checkout,
        {
            "action": "evaluate",
            "case_file": str(case_file),
            "full_db": str(layout.state_dir / "index.sqlite"),
            "synthetic_db": str(layout.synthetic_state_dir / "index.sqlite"),
        },
        request_dir,
        api_module=layout.api_module,
        home=layout.home,
        hermes_home=layout.hermes_home,
        source_root=layout.source_root,
        state_dir=layout.state_dir,
    )


def _label_results(
    frozen: FrozenUsageCorpus,
    output: Mapping[str, Any],
) -> tuple[dict[str, list[str]], set[str]]:
    raw_results = output.get("label_search", {})
    results: dict[str, list[str]] = {}
    errors: set[str] = set()
    queries = set(frozen.positive_labels) | {query for query, _artifact_id in frozen.negative_pairs}
    for query in queries:
        outcome = raw_results.get(sha256_text(query), {}) if isinstance(raw_results, dict) else {}
        if not isinstance(outcome, dict) or outcome.get("status") != "ok":
            results[query] = []
            errors.add(query)
        else:
            results[query] = [str(value) for value in outcome.get("ids", [])][:10]
    return results, errors


def _baseline_replay_failures(replay: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> list[str]:
    failures: list[str] = []
    for group_name in ("search", "get", "neighbors"):
        for case_id, outcome in replay.get(group_name, {}).items():
            if outcome.get("status") != "ok":
                failures.append(f"baseline_replay_error:{group_name}:{case_id}")
            elif group_name == "get" and outcome.get("payload") is None:
                failures.append(f"baseline_get_missing:{case_id}")
    return failures


def _build_ref_evaluation(
    layout: RefLayout,
    oracle: IndexOracle,
    frozen: FrozenUsageCorpus,
    case_payload: Mapping[str, Any],
    evaluator_output: dict[str, Any],
) -> RefEvaluation:
    search_results, search_errors = _label_results(frozen, evaluator_output)
    equivalents = parent_equivalence_map(oracle.artifacts)
    positive = compute_positive_evaluation(
        frozen.positive_labels,
        search_results,
        parent_equivalents=equivalents,
        errors=search_errors,
    )
    negative = compute_negative_evaluation(
        frozen.negative_pairs,
        search_results,
        errors=search_errors,
    )
    replay = evaluator_output.get("replay", {})
    if not isinstance(replay, dict):
        replay = {"search": {}, "get": {}, "neighbors": {}}
    synthetic_outcomes = evaluator_output.get("synthetic", {})
    if not isinstance(synthetic_outcomes, dict):
        synthetic_outcomes = {}
    synthetic = evaluate_synthetic_cases(case_payload["synthetic"], synthetic_outcomes)
    failures: list[str] = []
    if not oracle.valid:
        failures.extend(f"structure:{error}" for error in oracle.errors)
    if search_errors:
        failures.append("label_search_errors")
    if synthetic["failed"]:
        failures.append("synthetic_regressions")
    return RefEvaluation(
        layout.ref,
        layout.ref_key,
        layout.api_module,
        oracle,
        positive,
        negative,
        replay,
        synthetic,
        evaluator_output,
        failures,
        not failures,
    )


def _nearest_rank_p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return round(ordered[index], 3)


def timing_summary(evaluator_output: Mapping[str, Any]) -> dict[str, Any]:
    build_rows = evaluator_output.get("_builds", {})
    if not isinstance(build_rows, Mapping):
        build_rows = {}

    def build_duration(name: str) -> float | None:
        row = build_rows.get(name, {})
        value = row.get("duration_ms") if isinstance(row, Mapping) else None
        return round(float(value), 3) if isinstance(value, (int, float)) else None

    durations: list[float] = []
    groups: list[Any] = [evaluator_output.get("label_search", {}), evaluator_output.get("synthetic", {})]
    replay = evaluator_output.get("replay", {})
    if isinstance(replay, Mapping):
        groups.append(replay.get("search", {}))
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        for row in group.values():
            value = row.get("duration_ms") if isinstance(row, Mapping) else None
            if isinstance(value, (int, float)):
                durations.append(float(value))
    return {
        "full_build_ms": build_duration("full"),
        "synthetic_build_ms": build_duration("synthetic"),
        "search_count": len(durations),
        "search_p95_ms": _nearest_rank_p95(durations),
    }


def _positive_improved_queries(baseline: MetricEvaluation, candidate: MetricEvaluation) -> set[str]:
    baseline_cases = {str(case["query_id"]): case for case in baseline.cases}
    improved: set[str] = set()
    for case in candidate.cases:
        query_id = str(case["query_id"])
        base = baseline_cases.get(query_id)
        if base is None:
            continue
        if (
            _rank_value(case.get("exact_rank")) < _rank_value(base.get("exact_rank"))
            or _rank_value(case.get("parent_equiv_rank")) < _rank_value(base.get("parent_equiv_rank"))
        ):
            improved.add(query_id)
    return improved


def _accepted_labeled_order_changes(
    changed_case_ids: Sequence[str],
    replay_cases: Sequence[ReplaySearchCase],
    improved_query_ids: set[str],
) -> list[str]:
    cases = {case.case_id: case for case in replay_cases}
    accepted: list[str] = []
    for case_id in changed_case_ids:
        case = cases.get(case_id)
        if case is None or case.artifact_type is not None or case.limit not in (None, 10):
            continue
        if sha256_text(case.query) in improved_query_ids:
            accepted.append(case_id)
    return sorted(accepted)


def _candidate_comparison(baseline: RefEvaluation, candidate: RefEvaluation, frozen: FrozenUsageCorpus) -> dict[str, Any]:
    structure = compare_index_oracles(baseline.oracle, candidate.oracle)
    positive = _compare_metric_evaluations(baseline.positive, candidate.positive, positive=True)
    negative = _compare_metric_evaluations(baseline.negative, candidate.negative, positive=False)
    replay = compare_replay_outputs(baseline.replay, candidate.replay)

    improved_query_ids = _positive_improved_queries(baseline.positive, candidate.positive)
    accepted_order_changes = _accepted_labeled_order_changes(
        [*replay["order_changes"], *replay["new_nonempty"]],
        frozen.replay_search,
        improved_query_ids,
    )
    replay_blockers = {
        "order_changes": [value for value in replay["order_changes"] if value not in accepted_order_changes],
        "new_nonempty": [value for value in replay["new_nonempty"] if value not in accepted_order_changes],
        "new_empty": replay["new_empty"],
        "candidate_errors": replay["candidate_errors"],
        "baseline_errors": replay["baseline_errors"],
        "get_changes": replay["get_changes"],
        "neighbor_changes": replay["neighbor_changes"],
    }
    replay["accepted_order_changes"] = sorted(accepted_order_changes)
    replay["accepted"] = not any(replay_blockers.values())
    accepted = (
        structure["equal"]
        and positive["accepted"]
        and negative["accepted"]
        and replay["accepted"]
        and candidate.synthetic["failed"] == 0
        and not candidate.failures
    )
    return {
        "baseline_ref": baseline.ref,
        "candidate_ref": candidate.ref,
        "candidate_key": candidate.ref_key,
        "accepted": accepted,
        "structure": structure,
        "positive": positive,
        "negative": negative,
        "replay": replay,
        "synthetic": candidate.synthetic,
    }


def _ref_report(result: RefEvaluation) -> dict[str, Any]:
    replay = result.replay
    return {
        "ref": result.ref,
        "ref_key": result.ref_key,
        "api_module": result.api_module,
        "accepted": result.accepted,
        "structure": result.oracle.summary,
        "positive": result.positive.metrics,
        "negative": result.negative.metrics,
        "timing": timing_summary(result.evaluator_output),
        "synthetic": result.synthetic,
        "replay": {
            "search_count": len(replay.get("search", {})),
            "get_count": len(replay.get("get", {})),
            "neighbor_count": len(replay.get("neighbors", {})),
            "error_count": sum(
                1
                for group in replay.values()
                if isinstance(group, dict)
                for outcome in group.values()
                if isinstance(outcome, dict) and outcome.get("status") != "ok"
            ),
        },
        "failures": list(result.failures),
    }


def _details_for_ref(
    result: RefEvaluation,
    frozen: FrozenUsageCorpus,
    search_results: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    positive_cases = []
    by_query_id = {str(case["query_id"]): case for case in result.positive.cases}
    for query, accepted in frozen.positive_labels.items():
        case = dict(by_query_id[sha256_text(query)])
        case.update({"query": query, "accepted_ids": sorted(accepted), "top_ids": list(search_results.get(query, ()))})
        positive_cases.append(case)
    negative_cases = []
    by_pair_id = {str(case["pair_id"]): case for case in result.negative.cases}
    for query, artifact_id in frozen.negative_pairs:
        pair_id = _case_id("negative", (query, artifact_id))
        case = dict(by_pair_id[pair_id])
        case.update({"query": query, "artifact_id": artifact_id, "top_ids": list(search_results.get(query, ()))})
        negative_cases.append(case)
    return {
        "ref": result.ref,
        "positive_cases": positive_cases,
        "negative_cases": negative_cases,
        "replay": result.replay,
        "synthetic": result.evaluator_output.get("synthetic", {}),
    }


def compare_refs(args: argparse.Namespace, base_dir: Path) -> ComparisonRun:
    base_dir = ensure_private_directory(base_dir)
    created_worktrees: list[Path] = []
    layouts: list[RefLayout] = []
    try:
        baseline_ref = str(args.refs[0])
        baseline_key = f"01-{safe_ref_name(baseline_ref)}"
        baseline_checkout = prepare_worktree(baseline_ref, base_dir, created_worktrees, baseline_key)
        resolved = _resolve_baseline_config(baseline_checkout, args, base_dir)
        live_source = Path(str(resolved["source_root"])).resolve()
        live_state = Path(str(resolved["state_dir"])).resolve()
        live_hermes = Path(str(resolved["hermes_home"])).resolve()
        resolved_settings = resolved.get("settings")
        if not isinstance(resolved_settings, dict):
            raise RuntimeError("baseline config settings must be an object")
        if not live_source.is_dir():
            raise FileNotFoundError("effective source root does not exist")
        if not live_hermes.is_dir():
            raise FileNotFoundError("effective Hermes home does not exist")

        frozen_paths, snapshot_report = _freeze_inputs(
            live_source,
            live_hermes,
            live_state,
            base_dir,
            resolved_settings,
        )
        usage_snapshot = snapshot_usage_database(
            args.usage_db.expanduser().resolve(),
            base_dir / "frozen" / "usage.sqlite",
        )
        raw_usage = read_usage_corpus(usage_snapshot.backup_path, live_source)
        synthetic_paths, regression, synthetic_manifest = _materialize_synthetic(
            baseline_checkout,
            base_dir,
            ensure_private_directory(base_dir / "fixture-home"),
            live_hermes,
        )

        baseline_layout = _create_ref_layout(
            baseline_ref,
            baseline_key,
            baseline_checkout,
            DEFAULT_API_MODULE,
            base_dir,
            frozen_paths,
            synthetic_paths,
            resolved,
            live_source,
            live_hermes,
            live_state,
            root_was_explicit=args.root is not None,
        )
        layouts.append(baseline_layout)
        baseline_build = _build_ref(baseline_layout, base_dir / "requests")
        baseline_oracle = inspect_index(baseline_layout.state_dir)
        if not baseline_oracle.valid:
            raise RuntimeError("baseline index failed structural validation")
        frozen_usage = freeze_usage_corpus(raw_usage, set(baseline_oracle.artifacts))
        case_payload = _case_file_payload(frozen_usage, regression)
        case_file = base_dir / "frozen" / "cases.json"
        write_private_json(case_file, case_payload)
        baseline_output = _evaluate_ref(baseline_layout, case_file, base_dir / "requests")
        baseline_output["_builds"] = baseline_build.get("builds", {})
        baseline = _build_ref_evaluation(
            baseline_layout,
            baseline_oracle,
            frozen_usage,
            case_payload,
            baseline_output,
        )
        baseline.failures.extend(_baseline_replay_failures(baseline.replay))
        baseline.accepted = not baseline.failures
        evaluations = [baseline]

        for index, ref_value in enumerate(args.refs[1:], start=2):
            ref = str(ref_value)
            ref_key = f"{index:02d}-{safe_ref_name(ref)}"
            checkout = prepare_worktree(ref, base_dir, created_worktrees, ref_key)
            api_module = str(args.candidate_api_module or DEFAULT_API_MODULE)
            layout = _create_ref_layout(
                ref,
                ref_key,
                checkout,
                api_module,
                base_dir,
                frozen_paths,
                synthetic_paths,
                resolved,
                live_source,
                live_hermes,
                live_state,
                root_was_explicit=args.root is not None,
            )
            layouts.append(layout)
            candidate_build = _build_ref(layout, base_dir / "requests")
            oracle = inspect_index(layout.state_dir)
            output = _evaluate_ref(layout, case_file, base_dir / "requests")
            output["_builds"] = candidate_build.get("builds", {})
            evaluations.append(
                _build_ref_evaluation(layout, oracle, frozen_usage, case_payload, output)
            )

        comparisons = [
            _candidate_comparison(baseline, candidate, frozen_usage)
            for candidate in evaluations[1:]
        ]
        for candidate, comparison in zip(evaluations[1:], comparisons, strict=True):
            candidate.accepted = bool(comparison["accepted"])
            if not candidate.accepted:
                candidate.failures.append("candidate_acceptance_failure")
        accepted = baseline.accepted and all(comparison["accepted"] for comparison in comparisons)
        report = {
            "schema_version": 1,
            "accepted": accepted,
            "baseline_ref": baseline_ref,
            "snapshot": {
                **snapshot_report,
                "synthetic_source_sha256": synthetic_manifest["source_sha256"],
                "synthetic_runtime_sha256": synthetic_manifest["runtime_sha256"],
                "synthetic_cases_sha256": synthetic_manifest["cases_sha256"],
                "usage_source_query_only": usage_snapshot.query_only,
                "usage_source_counts_unchanged": usage_snapshot.before_counts == usage_snapshot.after_counts,
                "usage_table_counts": usage_snapshot.before_counts,
            },
            "frozen_cases": {
                "positive_query_count": len(frozen_usage.positive_labels),
                "positive_label_count": sum(len(value) for value in frozen_usage.positive_labels.values()),
                "negative_pair_count": len(frozen_usage.negative_pairs),
                "search_replay_count": len(frozen_usage.replay_search),
                "search_rebuild_requested_count": sum(case.rebuild_requested for case in frozen_usage.replay_search),
                "get_replay_count": len(frozen_usage.replay_get),
                "neighbor_replay_count": len(frozen_usage.replay_neighbors),
                "positive_query_ids": [sha256_text(query) for query in frozen_usage.positive_labels],
                "negative_pair_ids": [
                    _case_id("negative", (query, artifact_id))
                    for query, artifact_id in frozen_usage.negative_pairs
                ],
            },
            "results": [_ref_report(value) for value in evaluations],
            "comparisons": comparisons,
        }
        details: dict[str, Any] = {
            "live_source": str(live_source),
            "live_state": str(live_state),
            "live_hermes_home": str(live_hermes),
            "positive_labels": {query: sorted(value) for query, value in frozen_usage.positive_labels.items()},
            "negative_pairs": list(frozen_usage.negative_pairs),
            "replay_cases": {
                "search": [asdict(case) for case in frozen_usage.replay_search],
                "get": [asdict(case) for case in frozen_usage.replay_get],
                "neighbors": [asdict(case) for case in frozen_usage.replay_neighbors],
            },
            "refs": [],
        }
        for evaluation in evaluations:
            search_results, _errors = _label_results(frozen_usage, evaluation.evaluator_output)
            details["refs"].append(_details_for_ref(evaluation, frozen_usage, search_results))
        write_private_json(base_dir / "report.json", report)
        if args.details:
            write_private_json(base_dir / "details.json", details)
        return ComparisonRun(accepted, report, details)
    finally:
        cleanup_worktrees(created_worktrees, keep=bool(args.keep_work_dir))


# ---------------------------------------------------------------------------
# CLI formatting


def format_float(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def print_markdown(report: Mapping[str, Any], *, base_dir: Path | None = None) -> None:
    columns = [
        ("ref", "ref"),
        ("accepted", "pass"),
        ("artifacts", "artifacts"),
        ("queries", "queries"),
        ("hit10", "exact@10"),
        ("mrr", "mrr@10"),
        ("parent10", "parent@10"),
        ("bad10", "bad@10"),
        ("synthetic", "synthetic"),
        ("replay_errors", "replay errors"),
    ]
    print("| " + " | ".join(label for _key, label in columns) + " |")
    print("|" + "|".join("---" for _ in columns) + "|")
    for result in report.get("results", []):
        positive = result["positive"]
        negative = result["negative"]
        structure = result["structure"]
        synthetic = result["synthetic"]
        replay = result["replay"]
        row = {
            "ref": result["ref"],
            "accepted": "yes" if result["accepted"] else "NO",
            "artifacts": structure["artifact_count"],
            "queries": positive["query_count"],
            "hit10": positive["hit_at_10"],
            "mrr": positive["mrr_at_10"],
            "parent10": positive["parent_equiv_hit_at_10"],
            "bad10": negative["bad_hit_at_10"],
            "synthetic": f"{synthetic['passed']}/{synthetic['case_count']}",
            "replay_errors": replay["error_count"],
        }
        print("| " + " | ".join(format_float(row[key]) for key, _label in columns) + " |")
    print(f"\nAcceptance: {'PASS' if report.get('accepted') else 'FAIL'}")
    if base_dir is not None:
        print(f"Private evaluation directory: {base_dir}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("refs", nargs="+", help="Git refs/tags to compare; use WORKTREE for the current working tree")
    parser.add_argument("--usage-db", type=Path, required=True, help="usage.sqlite containing feedback labels and replay cases")
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path.home() / ".hermes",
        help="Hermes home used to resolve the frozen configured corpus",
    )
    parser.add_argument("--root", type=Path, default=None, help="Source root to index instead of the configured root")
    parser.add_argument("--work-dir", type=Path, default=None, help="Owner-only directory for private snapshots, refs, and reports")
    parser.add_argument("--keep-work-dir", action="store_true", help="Keep generated worktrees and private state for inspection")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a Markdown table")
    parser.add_argument("--details", action="store_true", help="Include raw private query/artifact details in JSON and details.json")
    parser.add_argument(
        "--candidate-api-module",
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def _work_dir(args: argparse.Namespace) -> tuple[Path, bool]:
    if args.work_dir is None:
        return ensure_private_directory(Path(tempfile.mkdtemp(prefix="local-knowledge-eval-"))), True
    requested = args.work_dir.expanduser().resolve()
    if _is_within(requested, REPO_ROOT.resolve()):
        raise ValueError("private evaluation work directory must be outside the repository")
    return ensure_private_directory(requested), False


def cleanup_private_directory(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except Exception as exc:
        print(f"WARNING: private evaluation cleanup failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    old_umask = os.umask(0o077)
    args = build_parser().parse_args(argv)
    base_dir: Path | None = None
    temporary = False
    try:
        base_dir, temporary = _work_dir(args)
        comparison = compare_refs(args, base_dir)
        payload = dict(comparison.report)
        if args.details:
            payload["details"] = comparison.details
        if args.keep_work_dir:
            payload["work_dir"] = str(base_dir)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_markdown(
                comparison.report,
                base_dir=base_dir if args.keep_work_dir or args.work_dir is not None else None,
            )
        return 0 if comparison.accepted else 1
    except Exception as exc:
        error_payload = {
            "accepted": False,
            "error_type": type(exc).__name__,
            "error_sha256": sha256_text(f"{type(exc).__name__}: {exc}"),
        }
        if args.json:
            print(json.dumps(error_payload, indent=2, sort_keys=True))
        else:
            print(f"historical comparison failed: {error_payload['error_type']}:{error_payload['error_sha256']}", file=sys.stderr)
        return 2
    finally:
        if temporary and base_dir is not None and not args.keep_work_dir:
            cleanup_private_directory(base_dir)
        os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())
