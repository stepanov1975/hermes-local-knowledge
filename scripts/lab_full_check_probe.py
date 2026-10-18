"""Trusted pytest fixture boundary for immutable full-candidate verification.

Only the install smoke's input tree is redirected: its assertions and Hermes'
security scanner remain unchanged. The test overlay is not a plugin artifact.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def tree_identity(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlink in full-check input")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def pytest_configure(config: object) -> None:
    root = Path("/check-inputs")
    receipt = json.loads((root / "inputs.json").read_text())
    from scripts.lab_check_inputs import validate_test_inputs
    validate_test_inputs(receipt)
    for key, path in (("candidate", root / "exact-candidate"),
                      ("overlay", Path("/candidate")), ("tools", root / "tools")):
        if tree_identity(path) != receipt[key]:
            raise ValueError("Full-check input identity mismatch: " + key)
    positive = subprocess.run(["jq", "-e", ".items | map(.n) | add == 5"],
                              input=b'{"items":[{"n":2},{"n":3}]}', capture_output=True, check=True)
    negative = subprocess.run(["jq", "-e", ".missing"], input=b'{}', capture_output=True, check=False)
    malformed = subprocess.run(["jq", "."], input=b'{', capture_output=True, check=False)
    if positive.stdout.strip() != b"true" or negative.returncode != 1 or malformed.returncode == 0:
        raise ValueError("Real jq behavioral prerequisite failed")
    print("Full-check exact export, trusted overlay, pinned jq bytes and positive/negative jq verified")
    version = subprocess.run(["jq", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    host = subprocess.run([sys.executable, "-c",
        "import hermes_cli, importlib.metadata as m; "
        "print(m.version('hermes-agent')); print(hermes_cli.__file__)"],
        cwd="/tmp", capture_output=True, text=True, check=True)
    print(json.dumps({"jq": version, "hermes_cli": shutil.which("hermes"),
                      "official_python": sys.executable, "official_host": host.stdout.splitlines()}, sort_keys=True))


def pytest_collection_modifyitems(items: list) -> None:
    matches = [item for item in items if item.nodeid ==
               "tests/test_install_smoke.py::test_hermes_plugin_install_register_and_search_smoke"]
    if len(matches) != 1:
        raise ValueError("Required original installation smoke was not collected")
    module = matches[0].module
    if Path(module.__file__).resolve() != Path("/candidate/tests/test_install_smoke.py"):
        raise ValueError("Unexpected installation smoke provenance")
    module.REPO_ROOT = Path("/check-inputs/exact-candidate")
