#!/usr/bin/env python3
"""Validate plugin version metadata and version-bump policy.

The plugin version is intentionally duplicated in three places because Hermes,
Python packaging, and runtime telemetry read different metadata surfaces. This
script keeps those copies synchronized and enforces a release bump when a PR or
push changes package/runtime-relevant files.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = (
    "plugin.yaml",
    "pyproject.toml",
    "hermes_local_knowledge/__init__.py",
)
RELEASE_RELEVANT_FILES = {
    "__init__.py",
    "after-install.md",
    "plugin.yaml",
    "pyproject.toml",
}
RELEASE_RELEVANT_PREFIXES = (
    "hermes_local_knowledge/",
    "examples/",
    "skills/",
)
_VERSION_RE = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_SIMPLE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_ZERO_SHA_RE = re.compile(r"^0+$")


class PolicyError(RuntimeError):
    """Raised when the version policy fails."""


@dataclass(frozen=True)
class VersionMetadata:
    """The version values read from all duplicated metadata locations."""

    plugin_yaml: str
    pyproject_toml: str
    package_init: str

    def as_dict(self) -> dict[str, str]:
        return {
            "plugin.yaml": self.plugin_yaml,
            "pyproject.toml": self.pyproject_toml,
            "hermes_local_knowledge/__init__.py": self.package_init,
        }


def _git(args: Sequence[str], *, root: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def parse_plugin_yaml_version(text: str) -> str:
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "version":
            return value.strip().strip("'\"")
    raise PolicyError("plugin.yaml does not contain a top-level version field")


def parse_pyproject_version(text: str) -> str:
    data = tomllib.loads(text)
    try:
        value = data["project"]["version"]
    except KeyError as exc:
        raise PolicyError("pyproject.toml does not contain project.version") from exc
    if not isinstance(value, str):
        raise PolicyError("pyproject.toml project.version must be a string")
    return value


def parse_package_init_version(text: str) -> str:
    match = _VERSION_RE.search(text)
    if match is None:
        raise PolicyError("hermes_local_knowledge/__init__.py does not define __version__")
    return match.group(1)


_VERSION_PARSERS: dict[str, Callable[[str], str]] = {
    "plugin.yaml": parse_plugin_yaml_version,
    "pyproject.toml": parse_pyproject_version,
    "hermes_local_knowledge/__init__.py": parse_package_init_version,
}


def _metadata_from_texts(texts: dict[str, str]) -> VersionMetadata:
    return VersionMetadata(
        plugin_yaml=_VERSION_PARSERS["plugin.yaml"](texts["plugin.yaml"]),
        pyproject_toml=_VERSION_PARSERS["pyproject.toml"](texts["pyproject.toml"]),
        package_init=_VERSION_PARSERS["hermes_local_knowledge/__init__.py"](
            texts["hermes_local_knowledge/__init__.py"]
        ),
    )


def read_current_metadata(root: Path) -> VersionMetadata:
    return _metadata_from_texts({path: (root / path).read_text(encoding="utf-8") for path in VERSION_FILES})


def read_metadata_at_ref(root: Path, ref: str) -> VersionMetadata:
    texts: dict[str, str] = {}
    for path in VERSION_FILES:
        result = _git(["show", f"{ref}:{path}"], root=root, check=False)
        if result.returncode != 0:
            raise PolicyError(f"Could not read {path!r} at {ref!r}: {result.stderr.strip()}")
        texts[path] = result.stdout
    return _metadata_from_texts(texts)


def require_metadata_in_sync(metadata: VersionMetadata, *, context: str = "Current") -> str:
    values = metadata.as_dict()
    unique_versions = set(values.values())
    if len(unique_versions) == 1:
        return next(iter(unique_versions))

    rendered = ", ".join(f"{path}={version}" for path, version in values.items())
    raise PolicyError(f"{context} plugin version metadata is not synchronized: {rendered}")


def simple_version_key(version: str) -> tuple[int, int, int]:
    match = _SIMPLE_VERSION_RE.fullmatch(version)
    if match is None:
        raise PolicyError(
            f"Unsupported version {version!r}; version policy expects simple MAJOR.MINOR.PATCH numbers"
        )
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def is_version_bumped(*, current: str, base: str) -> bool:
    return simple_version_key(current) > simple_version_key(base)


def is_release_relevant_path(path: str) -> bool:
    normalized = path.strip().lstrip("./")
    return normalized in RELEASE_RELEVANT_FILES or any(
        normalized.startswith(prefix) for prefix in RELEASE_RELEVANT_PREFIXES
    )


def _split_paths(stdout: str) -> set[str]:
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def changed_files(root: Path, *, base_ref: str, head_ref: str) -> list[str]:
    result = _git(["diff", "--name-only", f"{base_ref}...{head_ref}"], root=root)
    paths = _split_paths(result.stdout)

    # Local pre-commit runs usually compare origin/main with a dirty worktree.
    # CI checkouts are clean, so this only broadens local validation.
    if head_ref == "HEAD":
        worktree_result = _git(["diff", "--name-only", "HEAD"], root=root)
        paths.update(_split_paths(worktree_result.stdout))
        untracked_result = _git(["ls-files", "--others", "--exclude-standard"], root=root)
        paths.update(_split_paths(untracked_result.stdout))

    return sorted(paths)


def ref_exists(root: Path, ref: str) -> bool:
    result = _git(["cat-file", "-e", f"{ref}^{{commit}}"], root=root, check=False)
    return result.returncode == 0


def _usable_ref(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or _ZERO_SHA_RE.fullmatch(stripped):
        return None
    return stripped


def default_base_ref(root: Path) -> str | None:
    explicit = _usable_ref(os.environ.get("VERSION_POLICY_BASE_REF"))
    if explicit is not None:
        return explicit

    github_base = _usable_ref(os.environ.get("GITHUB_BASE_REF"))
    if github_base is not None:
        return f"origin/{github_base}"

    if ref_exists(root, "origin/main"):
        return "origin/main"
    return None


def check_version_policy(root: Path, *, base_ref: str | None, head_ref: str = "HEAD") -> list[str]:
    messages: list[str] = []

    current_metadata = read_current_metadata(root)
    current_version = require_metadata_in_sync(current_metadata)
    messages.append(f"Version metadata in sync: {current_version}")

    if base_ref is None:
        messages.append("No base ref available; skipping version-bump diff policy.")
        return messages

    if not ref_exists(root, base_ref):
        raise PolicyError(f"Base ref {base_ref!r} is not available. Check checkout fetch-depth/ref setup.")
    if not ref_exists(root, head_ref):
        raise PolicyError(f"Head ref {head_ref!r} is not available.")

    changed = changed_files(root, base_ref=base_ref, head_ref=head_ref)
    release_relevant = [path for path in changed if is_release_relevant_path(path)]
    if not changed:
        messages.append(f"No changed files compared with {base_ref}; version bump not required.")
        return messages
    if not release_relevant:
        messages.append("No release-relevant files changed; version bump not required.")
        return messages

    base_metadata = read_metadata_at_ref(root, base_ref)
    base_version = require_metadata_in_sync(base_metadata, context="Base")
    if not is_version_bumped(current=current_version, base=base_version):
        files = "\n  - ".join(release_relevant)
        raise PolicyError(
            "Release-relevant files changed without a plugin version bump.\n"
            f"Base version: {base_version}\n"
            f"Current version: {current_version}\n"
            "Release-relevant files:\n"
            f"  - {files}\n"
            "Bump plugin.yaml, pyproject.toml, and hermes_local_knowledge/__init__.py together."
        )

    messages.append(
        f"Release-relevant files changed and version was bumped: {base_version} -> {current_version}"
    )
    return messages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        default=None,
        help="Base git ref/sha for version-bump enforcement. Defaults to VERSION_POLICY_BASE_REF, "
        "GITHUB_BASE_REF, or origin/main when available.",
    )
    parser.add_argument("--head-ref", default="HEAD", help="Head git ref/sha to compare, default: HEAD")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Repository root, default: this repo")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    base_ref = _usable_ref(args.base_ref) or default_base_ref(root)

    try:
        messages = check_version_policy(root, base_ref=base_ref, head_ref=args.head_ref)
    except PolicyError as exc:
        print(f"version policy failed: {exc}", file=sys.stderr)
        return 1

    for message in messages:
        print(message)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
