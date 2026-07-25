from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_sdist_contains_files_required_by_shipped_tests(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", str(dist_dir)],
        cwd=PROJECT_ROOT,
        check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    archive_path = next(dist_dir.glob("*.tar.gz"))
    with tarfile.open(archive_path, "r:gz") as archive:
        members = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}

    required = {
        "plugin.yaml",
        "after-install.md",
        "scripts/__init__.py",
        "scripts/check_version_policy.py",
        "scripts/compare_historical_query_versions.py",
        "skills/local-knowledge-router/SKILL.md",
        "examples/local-knowledge-router-skill/SKILL.md",
        "tests/test_version_policy.py",
    }
    assert required <= members
