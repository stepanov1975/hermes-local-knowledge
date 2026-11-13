"""Launch workers through the runtime belonging to the loaded Hermes host."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


def hermes_command(args: Sequence[str], *, hermes_home: Path) -> list[str]:
    """Resolve managed bootstrap argv without pinning a dependency generation.

    Never use PATH: it can name a different Hermes installation. Legacy
    venv/PyPI hosts retain their interpreter-based module invocation.
    """
    hermes_cli = importlib.import_module("hermes_cli")

    if not hermes_cli.__file__:
        raise OSError("Loaded Hermes host has no source location")
    root = Path(hermes_cli.__file__).resolve().parent.parent
    bin_dir = root / ".hermes" / "bin"
    names = ("hermes.exe", "hermes.cmd") if os.name == "nt" else ("hermes",)
    launcher = next((bin_dir / name for name in names if (bin_dir / name).is_file()), None)
    if launcher is None:
        # Managed launchers use isolated store Python; raw -m would lose both
        # the checkout and bootstrap. A missing launcher must not mask damage.
        if bin_dir.is_dir() or sys.flags.isolated:
            raise OSError(f"Hermes runtime launcher missing under {bin_dir}")
        return [sys.executable, "-m", "hermes_cli.main", *args]

    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    try:
        result = subprocess.run(
            [str(launcher), "--print-runtime-command", "--", *args],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=True, timeout=10,
        )
        command = json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        raise OSError(f"Could not resolve Hermes runtime command from {launcher}") from exc
    if (not isinstance(command, list) or not command
            or not all(isinstance(part, str) and part and "\0" not in part for part in command)):
        raise OSError(f"Invalid Hermes runtime command from {launcher}")
    return command
