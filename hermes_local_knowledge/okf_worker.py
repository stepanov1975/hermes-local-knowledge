"""Bounded subprocess wrapper for detached OKF generation workers."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _release_lock(lock_path: str | None) -> None:
    if not lock_path:
        return
    try:
        Path(lock_path).unlink()
    except FileNotFoundError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a bounded Hermes OKF worker")
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--toolsets", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--lock-path")
    args = parser.parse_args(argv)

    command = [
        "hermes",
        "chat",
        "-Q",
        "--toolsets",
        args.toolsets,
        "--source",
        args.source,
        "--max-turns",
        "20",
        "-q",
        args.prompt,
    ]
    try:
        return subprocess.run(command, timeout=args.timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        print(f"OKF worker timed out after {args.timeout}s", file=sys.stderr)
        return 124
    finally:
        _release_lock(args.lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
