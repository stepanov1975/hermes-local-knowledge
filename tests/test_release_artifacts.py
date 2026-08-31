from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.verify_release_artifacts import (
    ReleaseArtifactError,
    validate_package_module_provenance,
    validate_sdist,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SDIST_MEMBERS = {
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "hermes_local_knowledge/__init__.py",
    "hermes_local_knowledge/plugin.py",
    "hermes_local_knowledge/skills/local-knowledge-router/SKILL.md",
    "hermes_local_knowledge.egg-info/entry_points.txt",
}


def write_sdist(
    path: Path,
    members: set[str] | list[str],
    *,
    directories: set[str] | None = None,
    member_types: dict[str, bytes] | None = None,
    raw_members: list[tarfile.TarInfo] | None = None,
) -> None:
    directories = directories or set()
    member_types = member_types or {}
    ordered_members = sorted(members) if isinstance(members, set) else members
    with tarfile.open(path, "w:gz") as archive:
        for name in ordered_members:
            member = tarfile.TarInfo(f"hermes_local_knowledge-1.0/{name}")
            if name in member_types:
                member.type = member_types[name]
                if member.type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                    member.linkname = "target"
                archive.addfile(member)
                continue
            if name in directories:
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
                continue
            payload = b"fixture\n"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        for raw_member in raw_members or []:
            archive.addfile(raw_member)


def test_sdist_validator_accepts_required_payload(tmp_path: Path) -> None:
    sdist = tmp_path / "package.tar.gz"
    write_sdist(sdist, REQUIRED_SDIST_MEMBERS)

    assert REQUIRED_SDIST_MEMBERS <= validate_sdist(sdist)


def test_sdist_validator_rejects_missing_required_payload(tmp_path: Path) -> None:
    sdist = tmp_path / "package.tar.gz"
    write_sdist(
        sdist,
        REQUIRED_SDIST_MEMBERS - {"hermes_local_knowledge.egg-info/entry_points.txt"},
    )

    with pytest.raises(ReleaseArtifactError, match="missing required package payload"):
        validate_sdist(sdist)


def test_sdist_validator_rejects_required_directory_member(tmp_path: Path) -> None:
    sdist = tmp_path / "package.tar.gz"
    write_sdist(sdist, REQUIRED_SDIST_MEMBERS, directories={"LICENSE"})

    with pytest.raises(ReleaseArtifactError, match="must be regular files"):
        validate_sdist(sdist)


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_sdist_validator_rejects_linked_and_special_members(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    sdist = tmp_path / "package.tar.gz"
    write_sdist(
        sdist,
        REQUIRED_SDIST_MEMBERS,
        member_types={"LICENSE": member_type},
    )

    with pytest.raises(ReleaseArtifactError, match="unsupported sdist member type"):
        validate_sdist(sdist)


@pytest.mark.parametrize("raw_name", ["", "."])
def test_sdist_validator_rejects_members_without_a_normalized_path(
    tmp_path: Path,
    raw_name: str,
) -> None:
    sdist = tmp_path / "package.tar.gz"
    raw_member = tarfile.TarInfo(raw_name)
    raw_member.type = tarfile.FIFOTYPE
    write_sdist(
        sdist,
        REQUIRED_SDIST_MEMBERS,
        raw_members=[raw_member],
    )

    with pytest.raises(ReleaseArtifactError, match="unsafe sdist member path"):
        validate_sdist(sdist)


def test_sdist_validator_rejects_duplicate_normalized_members(tmp_path: Path) -> None:
    sdist = tmp_path / "package.tar.gz"
    write_sdist(sdist, [*sorted(REQUIRED_SDIST_MEMBERS), "./LICENSE"])

    with pytest.raises(ReleaseArtifactError, match="duplicate normalized sdist member"):
        validate_sdist(sdist)


@pytest.mark.parametrize(
    ("forbidden_member", "is_directory"),
    [
        ("tests", True),
        ("tests/test_public_contract.py", False),
        ("scripts/evaluate_ref.py", False),
        (".github/workflows/release.yml", False),
    ],
)
def test_sdist_validator_rejects_repository_only_payload(
    tmp_path: Path,
    forbidden_member: str,
    is_directory: bool,
) -> None:
    sdist = tmp_path / "package.tar.gz"
    write_sdist(
        sdist,
        REQUIRED_SDIST_MEMBERS | {forbidden_member},
        directories={forbidden_member} if is_directory else set(),
    )

    with pytest.raises(ReleaseArtifactError, match="repository-only"):
        validate_sdist(sdist)


def test_package_module_provenance_rejects_checkout_import(tmp_path: Path) -> None:
    site_packages = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    installed_module = site_packages / "hermes_local_knowledge" / "__init__.py"
    checkout_module = tmp_path / "checkout" / "hermes_local_knowledge" / "plugin.py"
    installed_module.parent.mkdir(parents=True)
    checkout_module.parent.mkdir(parents=True)
    installed_module.write_text("", encoding="utf-8")
    checkout_module.write_text("", encoding="utf-8")
    modules = {
        "hermes_local_knowledge": SimpleNamespace(__file__=str(installed_module)),
        "hermes_local_knowledge.plugin": SimpleNamespace(__file__=str(checkout_module)),
    }

    with pytest.raises(ReleaseArtifactError, match="outside the smoke environment"):
        validate_package_module_provenance(modules, site_packages=(site_packages,))


def test_release_workflow_runs_archive_and_isolated_install_smokes() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "python scripts/verify_release_artifacts.py --sdist dist/*.tar.gz" in workflow
    assert "for artifact in dist/*.whl dist/*.tar.gz; do" in workflow
    assert 'mkdir "$smoke_root/empty"' in workflow
    assert 'cd "$smoke_root/empty"' in workflow
    assert "env -u PYTHONPATH -u VIRTUAL_ENV" in workflow
    assert '"$smoke_root/venv/bin/python" -I' in workflow
    assert '"$GITHUB_WORKSPACE/scripts/verify_release_artifacts.py" --installed-smoke' in workflow
