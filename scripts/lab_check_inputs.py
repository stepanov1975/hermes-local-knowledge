"""Explicit public fixtures consumed by trusted checks, never candidate inputs."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
import stat


EXTRA_TEST_INPUTS = (
    "docker/autonomous_lab/native_probe.py",
    ".github/workflows/security.yml",
)


def trusted_test_inputs(root: Path) -> dict[str, str]:
    result = {}
    for name in EXTRA_TEST_INPUTS:
        path = root / name
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("Trusted test input must not traverse symlinks")
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Trusted test input must be regular")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def overlay_test_inputs(trusted: Path, work: Path) -> dict[str, str]:
    """Overlay only the named public fixtures; fail closed on missing/drifting bytes."""
    expected = trusted_test_inputs(trusted)
    for name in EXTRA_TEST_INPUTS:
        target = work / name
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise ValueError("Trusted test input destination must not traverse symlinks")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(trusted / name, target)
    if trusted_test_inputs(work) != expected or trusted_test_inputs(trusted) != expected:
        raise ValueError("Trusted test input copy drift")
    return expected


def validate_test_inputs(inputs: dict) -> None:
    """Check receipt-only correspondence; no omitted source is exposed to reviewers."""
    trusted = inputs.get("trusted_test_inputs", {})
    if set(trusted) != set(EXTRA_TEST_INPUTS):
        raise ValueError("Trusted test input manifest must name exactly the required public files")
    for name, digest in trusted.items():
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or inputs.get("controller", {}).get(name) != digest
                or inputs.get("overlay", {}).get(name) != digest):
            raise ValueError("Trusted test input/controller/overlay mismatch")
