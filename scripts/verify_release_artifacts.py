#!/usr/bin/env python3
"""Validate publishable artifacts without trusting the source checkout."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import sys
import sysconfig
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath


PLUGIN_GROUP = "hermes_agent.plugins"
PLUGIN_NAME = "local_knowledge"
REQUIRED_SDIST_MEMBERS = frozenset(
    {
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "hermes_local_knowledge/__init__.py",
        "hermes_local_knowledge/plugin.py",
        "hermes_local_knowledge/skills/local-knowledge-router/SKILL.md",
        "hermes_local_knowledge.egg-info/entry_points.txt",
    }
)
REPOSITORY_ONLY_SDIST_PREFIXES = (".github", "scripts", "tests")


class ReleaseArtifactError(RuntimeError):
    """Raised when a release artifact violates the publication contract."""


def _relative_sdist_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    roots: set[str] = set()
    seen_archive_names: set[str] = set()
    relative_members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ReleaseArtifactError(f"unsafe sdist member path: {member.name!r}")
        parts = path.parts
        if not parts:
            raise ReleaseArtifactError(f"unsafe sdist member path: {member.name!r}")
        if not member.isfile() and not member.isdir():
            raise ReleaseArtifactError(
                f"unsupported sdist member type for {member.name!r}: {member.type!r}"
            )
        normalized_archive_name = PurePosixPath(*parts).as_posix()
        if normalized_archive_name in seen_archive_names:
            raise ReleaseArtifactError(
                f"duplicate normalized sdist member: {normalized_archive_name!r}"
            )
        seen_archive_names.add(normalized_archive_name)
        roots.add(parts[0])
        if len(parts) > 1:
            relative_name = PurePosixPath(*parts[1:]).as_posix()
            if relative_name in relative_members:
                raise ReleaseArtifactError(
                    f"duplicate normalized sdist member: {relative_name!r}"
                )
            relative_members[relative_name] = member
    if len(roots) != 1:
        raise ReleaseArtifactError(f"sdist must have exactly one archive root, found {sorted(roots)!r}")
    return relative_members


def validate_sdist(path: Path) -> set[str]:
    """Require a minimal package payload and reject repository-only content."""

    try:
        with tarfile.open(path, "r:gz") as archive:
            members = _relative_sdist_members(archive)
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseArtifactError(f"could not read sdist {path}: {exc}") from exc

    member_names = set(members)
    missing = sorted(REQUIRED_SDIST_MEMBERS - member_names)
    if missing:
        raise ReleaseArtifactError(f"sdist is missing required package payload: {missing!r}")

    non_regular_required = sorted(
        name for name in REQUIRED_SDIST_MEMBERS if not members[name].isfile()
    )
    if non_regular_required:
        raise ReleaseArtifactError(
            "required sdist package payload entries must be regular files: "
            f"{non_regular_required!r}"
        )

    repository_only = sorted(
        member
        for member in member_names
        if any(
            member == prefix or member.startswith(f"{prefix}/")
            for prefix in REPOSITORY_ONLY_SDIST_PREFIXES
        )
    )
    if repository_only:
        raise ReleaseArtifactError(
            f"sdist contains repository-only tests or tooling: {repository_only!r}"
        )
    return member_names


def validate_package_module_provenance(
    modules: Mapping[str, object],
    *,
    site_packages: Sequence[Path],
) -> dict[str, Path]:
    """Require every loaded package module to originate in this environment."""

    roots = tuple(path.resolve() for path in site_packages)
    if not roots:
        raise ReleaseArtifactError("could not determine smoke environment site-packages")

    package_modules = sorted(
        (name, module)
        for name, module in modules.items()
        if module is not None
        and (name == "hermes_local_knowledge" or name.startswith("hermes_local_knowledge."))
    )
    if not package_modules:
        raise ReleaseArtifactError("installed smoke did not load any package modules")

    resolved: dict[str, Path] = {}
    outside: list[str] = []
    for name, module in package_modules:
        raw_path = getattr(module, "__file__", None)
        if not isinstance(raw_path, str) or not raw_path:
            outside.append(f"{name}=<missing __file__>")
            continue
        module_path = Path(raw_path).resolve()
        resolved[name] = module_path
        if not any(module_path.is_relative_to(root) for root in roots):
            outside.append(f"{name}={module_path}")
    if outside:
        raise ReleaseArtifactError(
            "package modules loaded outside the smoke environment's site-packages: "
            + ", ".join(outside)
        )
    return resolved


def _installed_site_packages() -> tuple[Path, ...]:
    roots = {
        Path(value).resolve()
        for key in ("purelib", "platlib")
        if (value := sysconfig.get_path(key))
    }
    return tuple(sorted(roots, key=str))


def smoke_installed_artifact() -> dict[str, Path]:
    """Load the registered plugin first, then verify installed module provenance."""

    entries = [
        entry
        for entry in metadata.entry_points().select(group=PLUGIN_GROUP)
        if entry.name == PLUGIN_NAME
    ]
    if len(entries) != 1:
        raise ReleaseArtifactError(
            f"expected one {PLUGIN_NAME!r} entry point in {PLUGIN_GROUP!r}, found {entries!r}"
        )

    plugin_module = entries[0].load()
    if not callable(getattr(plugin_module, "register", None)):
        raise ReleaseArtifactError("plugin register entry point is not callable")

    okf_module = importlib.import_module("hermes_local_knowledge.okf")
    if not callable(getattr(okf_module, "run_worker", None)):
        raise ReleaseArtifactError("packaged OKF worker entry point is not callable")

    return validate_package_module_provenance(
        sys.modules,
        site_packages=_installed_site_packages(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sdist", type=Path, help="source distribution to validate")
    mode.add_argument(
        "--installed-smoke",
        action="store_true",
        help="smoke the package installed in the current Python environment",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.sdist is not None:
            members = validate_sdist(args.sdist)
            print(f"Validated sdist payload: {len(members)} members")
        else:
            modules = smoke_installed_artifact()
            print(f"Validated installed package provenance: {len(modules)} modules")
    except (ImportError, metadata.PackageNotFoundError, ReleaseArtifactError) as exc:
        print(f"Release artifact verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
