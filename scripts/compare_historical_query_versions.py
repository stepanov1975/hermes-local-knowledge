#!/usr/bin/env python3
"""Compare local-knowledge search quality across git refs.

The helper intentionally evaluates each ref with that ref's own search code. It
uses the target ref to build the index, then replays positive feedback labels
from a supplied ``usage.sqlite`` database against that target ref's
``search_index`` implementation.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

EVALUATOR_CODE = r'''
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from hermes_local_knowledge.indexer import search_index
except ImportError:  # pragma: no cover - compatibility fallback for older ref layouts
    from hermes_local_knowledge.search import search_index

POSITIVE = {"useful", "great"}
IGNORED = {"", "none", "null", "xxxx", "sentinel unlikely", "demo"}


def clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in IGNORED else text


def artifact_ids(db_path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {str(row[0]) for row in conn.execute("SELECT id FROM artifacts").fetchall()}
    finally:
        conn.close()


def table_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def labels_from_usage(usage_db: Path, valid: set[str]) -> dict[str, set[str]]:
    conn = sqlite3.connect(f"file:{usage_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT f.rating, COALESCE(f.query, e.query) AS query, f.artifact_id
            FROM feedback f
            LEFT JOIN usage_events e ON e.id = f.event_id
            WHERE f.rating IN ('useful', 'great')
              AND COALESCE(f.query, e.query) IS NOT NULL
              AND f.artifact_id IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()

    labels: dict[str, set[str]] = {}
    for row in rows:
        if clean(row["rating"]).lower() not in POSITIVE:
            continue
        query = clean(row["query"])
        artifact_id = clean(row["artifact_id"])
        if query and artifact_id and artifact_id in valid:
            labels.setdefault(query, set()).add(artifact_id)
    return labels


def parent_equivalents(db_path: Path) -> dict[str, set[str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, type, related_json FROM artifacts").fetchall()
    finally:
        conn.close()
    out: dict[str, set[str]] = {}
    for row in rows:
        artifact_id = str(row["id"])
        if row["type"] != "skill_support_doc":
            continue
        try:
            related = json.loads(row["related_json"] or "[]")
        except json.JSONDecodeError:
            related = []
        for item in related:
            related_id = str(item)
            if related_id.startswith("skill:"):
                out.setdefault(artifact_id, set()).add(related_id)
                out.setdefault(related_id, set()).add(artifact_id)
    return out


def matches_parent(result_id: str, expected: set[str], equivalents: dict[str, set[str]]) -> bool:
    return result_id in expected or bool(equivalents.get(result_id, set()) & expected)


def metrics_for(db_path: Path, usage_db: Path) -> dict[str, Any]:
    valid = artifact_ids(db_path)
    labels = labels_from_usage(usage_db, valid)
    equivalents = parent_equivalents(db_path)
    counters = {1: 0, 3: 0, 5: 0, 10: 0}
    parent_counters = {1: 0, 3: 0, 5: 0, 10: 0}
    reciprocal_rank = 0.0
    parent_reciprocal_rank = 0.0
    cases = []
    for query, expected in labels.items():
        result_ids = [str(row["id"]) for row in search_index(db_path, query, limit=10)]
        exact_rank = None
        parent_rank = None
        for rank, result_id in enumerate(result_ids[:10], start=1):
            if exact_rank is None and result_id in expected:
                exact_rank = rank
            if parent_rank is None and matches_parent(result_id, expected, equivalents):
                parent_rank = rank
        for k in counters:
            if exact_rank is not None and exact_rank <= k:
                counters[k] += 1
            if parent_rank is not None and parent_rank <= k:
                parent_counters[k] += 1
        if exact_rank is not None:
            reciprocal_rank += 1 / exact_rank
        if parent_rank is not None:
            parent_reciprocal_rank += 1 / parent_rank
        cases.append(
            {
                "query": query,
                "expected_ids": sorted(expected),
                "exact_rank": exact_rank,
                "parent_equiv_rank": parent_rank,
                "top_ids": result_ids[:10],
            }
        )
    denominator = len(labels) or 1
    return {
        "artifact_count": len(valid),
        "edge_count": table_count(db_path, "edges"),
        "query_count": len(labels),
        "label_count": sum(len(value) for value in labels.values()),
        "hit_at_1": counters[1] / denominator,
        "hit_at_3": counters[3] / denominator,
        "hit_at_5": counters[5] / denominator,
        "hit_at_10": counters[10] / denominator,
        "mrr_at_10": reciprocal_rank / denominator,
        "parent_equiv_hit_at_1": parent_counters[1] / denominator,
        "parent_equiv_hit_at_3": parent_counters[3] / denominator,
        "parent_equiv_hit_at_5": parent_counters[5] / denominator,
        "parent_equiv_hit_at_10": parent_counters[10] / denominator,
        "parent_equiv_mrr_at_10": parent_reciprocal_rank / denominator,
        "cases": cases,
    }


if __name__ == "__main__":
    print(json.dumps(metrics_for(Path(sys.argv[1]), Path(sys.argv[2])), indent=2, sort_keys=True))
'''


@dataclass(frozen=True)
class RefResult:
    ref: str
    worktree: Path
    state_dir: Path
    metrics: dict[str, Any]

    def as_dict(self, *, include_details: bool) -> dict[str, Any]:
        payload = dict(self.metrics)
        if not include_details:
            payload.pop("cases", None)
        payload.update({"ref": self.ref, "state_dir": str(self.state_dir), "worktree": str(self.worktree)})
        return payload


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
        raise RuntimeError(f"Command failed ({result.returncode}): {rendered}\n{result.stderr.strip()}")
    return result.stdout


def safe_ref_name(ref: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", ref.strip())
    stem = cleaned.strip("-.") or "ref"
    digest = hashlib.sha1(ref.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}"


def build_index_for_ref(
    worktree: Path,
    state_dir: Path,
    args: argparse.Namespace,
) -> None:
    hermes_home = args.hermes_home.expanduser().resolve()
    command = [
        sys.executable,
        "-m",
        "hermes_local_knowledge.indexer",
        "build",
        "--from-hermes-config",
        "--hermes-home",
        str(hermes_home),
        "--output-dir",
        str(state_dir),
    ]
    env = dict(os.environ)
    env["LOCAL_KNOWLEDGE_STATE_DIR"] = str(state_dir)
    env["HERMES_HOME"] = str(hermes_home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if args.root is not None:
        source_root = args.root.expanduser().resolve()
        command.extend(["--root", str(source_root)])
        env["LOCAL_KNOWLEDGE_ROOT"] = str(source_root)
    else:
        env.pop("LOCAL_KNOWLEDGE_ROOT", None)
    run(command, cwd=worktree, env=env)


def evaluate_ref(worktree: Path, state_dir: Path, usage_db: Path, evaluator: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(worktree) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    stdout = run(
        [sys.executable, str(evaluator), str(state_dir / "index.sqlite"), str(usage_db)],
        cwd=worktree,
        env=env,
    )
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("evaluation runner did not return a JSON object")
    return payload


def prepare_worktree(ref: str, base_dir: Path, created: list[Path], ref_key: str) -> Path:
    if ref in {"WORKTREE", "."}:
        return REPO_ROOT
    worktree = base_dir / "worktrees" / ref_key
    worktree.parent.mkdir(parents=True, exist_ok=True)
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
        except RuntimeError as exc:
            errors.append(str(exc))
    try:
        run(["git", "worktree", "prune"], cwd=REPO_ROOT)
    except RuntimeError as exc:
        errors.append(str(exc))
    for error in errors:
        print(f"WARNING: worktree cleanup failed: {error}", file=sys.stderr)


def compare_refs(args: argparse.Namespace, base_dir: Path) -> list[RefResult]:
    evaluator = base_dir / "historical_eval_runner.py"
    evaluator.write_text(EVALUATOR_CODE, encoding="utf-8")
    results: list[RefResult] = []
    created_worktrees: list[Path] = []
    try:
        for index, ref in enumerate(args.refs, start=1):
            ref_key = f"{index:02d}-{safe_ref_name(ref)}"
            worktree = prepare_worktree(ref, base_dir, created_worktrees, ref_key)
            state_dir = base_dir / "state" / ref_key
            state_dir.mkdir(parents=True, exist_ok=True)
            build_index_for_ref(worktree, state_dir, args)
            metrics = evaluate_ref(worktree, state_dir, args.usage_db.expanduser().resolve(), evaluator)
            results.append(RefResult(ref=ref, worktree=worktree, state_dir=state_dir, metrics=metrics))
    finally:
        cleanup_worktrees(created_worktrees, keep=bool(args.keep_work_dir))
    return results


def format_float(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def print_markdown(results: list[RefResult]) -> None:
    columns = [
        ("ref", "ref"),
        ("artifact_count", "artifacts"),
        ("edge_count", "edges"),
        ("query_count", "queries"),
        ("label_count", "labels"),
        ("hit_at_1", "exact@1"),
        ("hit_at_3", "exact@3"),
        ("hit_at_5", "exact@5"),
        ("hit_at_10", "exact@10"),
        ("mrr_at_10", "mrr@10"),
        ("parent_equiv_hit_at_1", "parent@1"),
        ("parent_equiv_hit_at_3", "parent@3"),
        ("parent_equiv_hit_at_5", "parent@5"),
        ("parent_equiv_hit_at_10", "parent@10"),
        ("parent_equiv_mrr_at_10", "parent_mrr"),
    ]
    print("| " + " | ".join(label for _key, label in columns) + " |")
    print("|" + "|".join("---" for _ in columns) + "|")
    for result in results:
        row = {"ref": result.ref, **result.metrics}
        print("| " + " | ".join(format_float(row.get(key, "")) for key, _label in columns) + " |")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("refs", nargs="+", help="Git refs/tags to compare; use WORKTREE for the current working tree")
    parser.add_argument("--usage-db", type=Path, required=True, help="usage.sqlite containing feedback labels")
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes", help="Hermes home for --from-hermes-config builds")
    parser.add_argument("--root", type=Path, default=None, help="Source root to index instead of reading Hermes config")
    parser.add_argument("--work-dir", type=Path, default=None, help="Directory for temporary worktrees and state")
    parser.add_argument("--keep-work-dir", action="store_true", help="Keep generated worktrees/state for inspection")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a Markdown table")
    parser.add_argument("--details", action="store_true", help="Include per-query replay details in JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_dir = args.work_dir.expanduser().resolve() if args.work_dir is not None else Path(tempfile.mkdtemp(prefix="local-knowledge-eval-"))
    base_dir.mkdir(parents=True, exist_ok=True)
    try:
        results = compare_refs(args, base_dir)
        if args.json:
            print(json.dumps({"results": [result.as_dict(include_details=args.details) for result in results]}, indent=2, sort_keys=True))
        else:
            print_markdown(results)
            if args.keep_work_dir or args.work_dir is not None:
                print(f"\nState directory: {base_dir}")
    finally:
        if not args.keep_work_dir and args.work_dir is None:
            shutil.rmtree(base_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
