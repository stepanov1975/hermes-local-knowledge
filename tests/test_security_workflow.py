"""Execute the CI audit shell, without a network or an installed scanner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml  # type: ignore[import-untyped]


SCANNER = "requirements-security.txt"
APPROVED_IDS = (
    "CVE-2026-59951",
    "CVE-2026-116684",
    "CVE-2026-116685",
    "CVE-2026-116686",
    "CVE-2026-114603",
)
ROOT = Path(__file__).resolve().parents[1]
# Include manifests on both sides of the scanner in shell glob order to catch
# leaking array state, plus any future root requirements manifests.
REQUIREMENTS = sorted(
    {p.name for p in ROOT.glob("requirements*.txt")}
    | {"requirements.txt", "requirements-aaa.txt", SCANNER, "requirements-zzz.txt"}
)
PROJECT = "project-dependencies.txt"
TARGETS = [*REQUIREMENTS, PROJECT]
pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="CI audit recipe requires POSIX executable shims and Bash",
)


def _run_audit(
    tmp_path: Path, *, vulnerable_target: str = SCANNER,
    advisories: tuple[str, ...] = APPROVED_IDS,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/security.yml").read_text())
    steps = workflow["jobs"]["python-security"]["steps"]
    recipe = next(step["run"] for step in steps if step["name"] == "Run pip-audit")
    for name in REQUIREMENTS:
        (tmp_path / name).write_text("fixture-dependency==1.0\n")
    runner_temp = tmp_path / "runner temp"
    runner_temp.mkdir()
    (runner_temp / PROJECT).write_text("fixture-project-dependency==1.0\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = bin_dir / "pip-audit"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['AUDIT_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "target = Path(args[args.index('-r') + 1]).name\n"
        "ignored = {args[i + 1] for i, arg in enumerate(args) if arg == '--ignore-vuln'}\n"
        "findings = json.loads(os.environ['AUDIT_FINDINGS'])\n"
        "remaining = set(findings.get(target, [])) - ignored\n"
        "sys.exit(1 if remaining else 0)\n"
    )
    recorder.chmod(0o755)
    log = tmp_path / "audit-log.jsonl"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "RUNNER_TEMP": str(runner_temp),
        "AUDIT_LOG": str(log),
        "AUDIT_FINDINGS": json.dumps({vulnerable_target: advisories}),
        "LC_ALL": "C",
    }
    # Match Actions' Bash error/pipe handling, in addition to the recipe's own
    # set -euo pipefail. Do not wrap the recipe in a success-masking shell.
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", recipe],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=20,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def _target(args: list[str]) -> str:
    return Path(args[args.index("-r") + 1]).name


def test_audit_exceptions_are_exact_and_scanner_only(tmp_path: Path) -> None:
    result, calls = _run_audit(tmp_path)
    assert result.returncode == 0, result.stderr
    assert [_target(args) for args in calls] == TARGETS
    for args in calls:
        expected = ["--progress-spinner", "off"]
        if _target(args) == SCANNER:
            for advisory in APPROVED_IDS:
                expected.extend(["--ignore-vuln", advisory])
        expected.extend(["-r", args[-1]])
        assert args == expected


@pytest.mark.parametrize(
    ("target", "advisory"),
    [(SCANNER, "CVE-2099-UNAPPROVED-TEST-ADVISORY")]
    + [(target, advisory) for target in TARGETS if target != SCANNER for advisory in APPROVED_IDS],
)
def test_audit_findings_fail_fast(
    tmp_path: Path, target: str, advisory: str,
) -> None:
    result, calls = _run_audit(tmp_path, vulnerable_target=target, advisories=(advisory,))
    assert result.returncode != 0, (target, advisory, result.stdout, result.stderr)
    # No subsequent audit may execute, including the generated project audit.
    assert [_target(args) for args in calls] == TARGETS[: TARGETS.index(target) + 1]
