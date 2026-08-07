#!/usr/bin/env python3
"""Render and verify one GitHub release body from CHANGELOG.md."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
RELEASE_HEADING_RE = re.compile(r"^## \[(?P<version>[^]]+)] - \d{4}-\d{2}-\d{2}$")
ANY_RELEASE_HEADING_RE = re.compile(r"^## \[[^]]+](?:\s|$)")
ANY_VERSION_LINK_RE = re.compile(r"^\[\d+\.\d+\.\d+]:\s*\S+\s*$")


class ReleaseNotesError(ValueError):
    """The changelog cannot produce one unambiguous release body."""


def render_release_notes(changelog_path: Path, version: str) -> str:
    """Render the changelog section and compare link for ``version``."""

    if VERSION_RE.fullmatch(version) is None:
        raise ReleaseNotesError(f"invalid release version: {version!r}")

    try:
        lines = changelog_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseNotesError(
            f"failed to read {changelog_path}: {type(exc).__name__}: {exc}"
        ) from exc
    starts = [
        index
        for index, line in enumerate(lines)
        if (match := RELEASE_HEADING_RE.fullmatch(line)) is not None
        and match.group("version") == version
    ]
    if not starts:
        raise ReleaseNotesError(f"no changelog section found for {version}")
    if len(starts) != 1:
        raise ReleaseNotesError(
            f"expected exactly one changelog section for {version}, found {len(starts)}"
        )

    start = starts[0] + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if ANY_RELEASE_HEADING_RE.match(lines[index]) is not None
        ),
        len(lines),
    )
    link_re = re.compile(rf"^\[{re.escape(version)}]:\s*(\S+)\s*$")
    section = [line for line in lines[start:end] if ANY_VERSION_LINK_RE.fullmatch(line) is None]
    while section and not section[0].strip():
        section.pop(0)
    while section and not section[-1].strip():
        section.pop()
    if not section:
        raise ReleaseNotesError(f"changelog section for {version} is empty")

    rendered_section = [f"## {line[4:]}" if line.startswith("### ") else line for line in section]
    links = [match.group(1) for line in lines if (match := link_re.fullmatch(line)) is not None]
    if len(links) != 1:
        raise ReleaseNotesError(
            f"expected exactly one changelog compare link for {version}, found {len(links)}"
        )

    compare_label = links[0].rstrip("/").rsplit("/", 1)[-1]
    return "\n".join(
        [
            *rendered_section,
            "",
            f"**Full changelog:** [{compare_label}]({links[0]})",
            "",
        ]
    )


def _read_release_json(release_json_path: Path) -> dict[str, object]:
    try:
        release = json.loads(release_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseNotesError(
            f"failed to read {release_json_path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(release, dict):
        raise ReleaseNotesError("release JSON must be an object")
    return release


def verify_release_body(release_json_path: Path, expected: str) -> None:
    """Require the GitHub release JSON body to match the rendered notes exactly."""

    release = _read_release_json(release_json_path)
    actual = release.get("body")
    if not isinstance(actual, str):
        raise ReleaseNotesError("release JSON has no string body")
    if actual != expected:
        raise ReleaseNotesError("published release body does not match CHANGELOG.md")


def inspect_release(
    release_json_path: Path,
    expected_notes: str,
    *,
    expected_wheel: str,
    expected_sdist: str,
) -> dict[str, bool]:
    """Classify release repair work while preserving unrelated assets."""

    release = _read_release_json(release_json_path)
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ReleaseNotesError("release JSON has no assets list")

    def asset_complete(expected_name: str) -> bool:
        matching = [
            asset
            for asset in assets
            if isinstance(asset, dict) and asset.get("name") == expected_name
        ]
        return (
            len(matching) == 1
            and matching[0].get("state") == "uploaded"
            and type(matching[0].get("size")) is int
            and matching[0]["size"] > 0
        )

    assets_complete = all(asset_complete(name) for name in (expected_wheel, expected_sdist))
    release_shape_complete = (
        release.get("isDraft") is False and release.get("isPrerelease") is False
    )
    notes_match = release.get("body") == expected_notes
    return {
        "assets_complete": assets_complete,
        "body_needs_update": not notes_match,
        "build_needed": not assets_complete,
        "needed": not (assets_complete and notes_match and release_shape_complete),
        "notes_match": notes_match,
        "release_shape_complete": release_shape_complete,
    }


def verify_release_complete(
    release_json_path: Path,
    expected_notes: str,
    *,
    expected_wheel: str,
    expected_sdist: str,
) -> None:
    """Require a published release with both expected artifacts and exact notes."""

    state = inspect_release(
        release_json_path,
        expected_notes,
        expected_wheel=expected_wheel,
        expected_sdist=expected_sdist,
    )
    missing = [
        name
        for name in ("assets_complete", "notes_match", "release_shape_complete")
        if not state[name]
    ]
    if missing:
        raise ReleaseNotesError("published release is incomplete: " + ", ".join(missing))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--release-json",
        type=Path,
        default=None,
        help="optional gh release JSON file whose body must match",
    )
    parser.add_argument("--expected-wheel")
    parser.add_argument("--expected-sdist")
    parser.add_argument("--inspect-release", action="store_true")
    parser.add_argument("--verify-complete", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        expected = render_release_notes(args.changelog, args.version)
        args.output.write_text(expected, encoding="utf-8")
        if args.inspect_release or args.verify_complete:
            if args.release_json is None or not args.expected_wheel or not args.expected_sdist:
                raise ReleaseNotesError(
                    "release inspection requires --release-json, --expected-wheel, "
                    "and --expected-sdist"
                )
            state = inspect_release(
                args.release_json,
                expected,
                expected_wheel=args.expected_wheel,
                expected_sdist=args.expected_sdist,
            )
            if args.inspect_release:
                print(json.dumps(state, sort_keys=True))
            if args.verify_complete:
                verify_release_complete(
                    args.release_json,
                    expected,
                    expected_wheel=args.expected_wheel,
                    expected_sdist=args.expected_sdist,
                )
        elif args.release_json is not None:
            verify_release_body(args.release_json, expected)
    except (OSError, UnicodeError, json.JSONDecodeError, ReleaseNotesError) as exc:
        print(f"release notes verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
