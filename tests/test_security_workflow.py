"""Scope the non-applicable scanner-server advisory exception, not project audits."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Exercises the Ubuntu security job's Bash recipe")
def test_mcp_http_server_exception_is_scanner_manifest_only(tmp_path):
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/security.yml").read_text()
    step = workflow.split("      - name: Run pip-audit\n", 1)[1]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    assert "semgrep scan --config p/ci --config p/secrets --error --metrics=off ." in workflow
    calls = tmp_path / "audit-calls.jsonl"
    fake = tmp_path / "pip-audit.py"
    fake.write_text("import json, os, sys\n"
        "with open(os.environ['TEST_AUDIT_LOG'], 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n")
    for name in ("requirements-release.txt", "requirements-security.txt", "requirements-other.txt"):
        (tmp_path / name).write_text("example==1\n")
    (tmp_path / "project-dependencies.txt").write_text("example==1\n")
    env = {**os.environ, "TEST_AUDIT_PYTHON": sys.executable, "TEST_AUDIT_SCRIPT": str(fake),
           "TEST_AUDIT_LOG": str(calls), "RUNNER_TEMP": str(tmp_path)}
    # The fixed read-only full-check worker uses a noexec /tmp. Invoke the fake
    # through Python without relaxing that isolation; the workflow is unchanged.
    script = 'pip-audit() { "$TEST_AUDIT_PYTHON" "$TEST_AUDIT_SCRIPT" "$@"; }\n' + script
    result = subprocess.run([shutil.which("bash") or "/bin/bash", "-c", script],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(recorded) == 4
    for argv in recorded:
        target = argv[argv.index("-r") + 1]
        if target == "requirements-security.txt":
            assert "--ignore-vuln" in argv
            assert argv[argv.index("--ignore-vuln") + 1] == "CVE-2026-59951"
        else:
            assert "--ignore-vuln" not in argv
