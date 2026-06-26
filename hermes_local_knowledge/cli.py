"""Command-line interface for the local knowledge indexer."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .constants import DEFAULT_ROOT
from .paths import default_output_dir, hermes_home_from_env
from .search import search_index
from .storage import build_index, get_artifact, get_neighbors


def print_results(rows: Sequence[dict[str, Any]]) -> None:
    for row in rows:
        print(f"{row['id']} [{row['type']}] {row['title']}")
        print(f"  path: {row['path']}")
        print(f"  summary: {row['summary']}")
        if row.get("edge_kind"):
            print(f"  edge: {row['edge_kind']} ({row.get('edge_evidence', '')})")
        if row.get("triggers"):
            print(f"  triggers: {', '.join(row['triggers'][:12])}")
        print()

def add_common_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=default_output_dir() / "index.sqlite", help="SQLite index path")

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="build index.sqlite and index.jsonl")
    build_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="source directory to index")
    build_parser.add_argument("--hermes-home", type=Path, default=hermes_home_from_env(), help="Hermes home directory")
    build_parser.add_argument("--output-dir", type=Path, default=None, help="output directory (default: <hermes-home>/local_knowledge)")

    search_parser = subparsers.add_parser("search", help="search artifacts")
    search_parser.add_argument("query", help="search query")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--json", action="store_true", help="emit JSON")
    add_common_db_arg(search_parser)

    get_parser = subparsers.add_parser("get", help="show one artifact by id")
    get_parser.add_argument("artifact_id")
    get_parser.add_argument("--json", action="store_true", help="emit JSON")
    add_common_db_arg(get_parser)

    neighbors_parser = subparsers.add_parser("neighbors", help="show graph neighbors for one artifact")
    neighbors_parser.add_argument("artifact_id")
    neighbors_parser.add_argument("--json", action="store_true", help="emit JSON")
    add_common_db_arg(neighbors_parser)
    return parser.parse_args(argv)

def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "build":
        output_dir = args.output_dir if args.output_dir is not None else default_output_dir(args.hermes_home)
        artifacts, edges = build_index(args.root, output_dir, args.hermes_home)
        counts: dict[str, int] = {}
        for artifact in artifacts:
            counts[artifact.type] = counts.get(artifact.type, 0) + 1
        print(f"Built {len(artifacts)} artifacts and {len(edges)} edges")
        for artifact_type, count in sorted(counts.items()):
            print(f"  {artifact_type}: {count}")
        print(f"SQLite: {output_dir / 'index.sqlite'}")
        print(f"JSONL:  {output_dir / 'index.jsonl'}")
        return 0

    if args.command == "search":
        rows = search_index(args.db, args.query, limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_results(rows)
        return 0

    if args.command == "get":
        row = get_artifact(args.db, args.artifact_id)
        if row is None:
            print(f"Artifact not found: {args.artifact_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_results([row])
        return 0

    if args.command == "neighbors":
        rows = get_neighbors(args.db, args.artifact_id)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_results(rows)
        return 0

    return 1
