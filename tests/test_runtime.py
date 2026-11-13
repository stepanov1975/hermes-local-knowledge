from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from hermes_local_knowledge import okf, shadow_hooks, shadow_supervisor
from hermes_local_knowledge import worker_launch as runtime
from hermes_local_knowledge.config import resolve_config


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "loaded-host"
    package = root / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.__file__ = str(package / "__init__.py")
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(executable=sys.executable, flags=SimpleNamespace(isolated=0)))
    return root


def launcher_path(host: Path) -> Path:
    launcher = host / ".hermes" / "bin" / ("hermes.exe" if os.name == "nt" else "hermes")
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.touch()
    return launcher


def test_legacy_host_uses_own_interpreter_not_path(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "different-hermes"))
    assert runtime.hermes_command(["local-knowledge", "okf-worker"], hermes_home=tmp_path) == [
        sys.executable, "-m", "hermes_cli.main", "local-knowledge", "okf-worker",
    ]


def test_managed_command_uses_loaded_source_and_explicit_profile(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = launcher_path(host)
    prefix = ["/store/python", "-I", "-c", "import hermes_bootstrap"]
    args = ["local-knowledge", "okf-worker", "--hermes-home", str(tmp_path)]
    monkeypatch.setenv("PATH", str(tmp_path / "different-hermes"))
    monkeypatch.setenv("HERMES_HOME", "/other-profile")

    def run(command: list[str], **kwargs: Any) -> Any:
        assert command == [str(launcher), "--print-runtime-command", "--", *args]
        assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)
        assert kwargs["timeout"] == 10 and kwargs["check"] is True
        return SimpleNamespace(stdout=json.dumps(prefix + args))

    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime.hermes_command(args, hermes_home=tmp_path) == prefix + args


@pytest.mark.parametrize("output", ["not json", "{}", "[]", '["python", 1]', '[""]', '["py\\u0000thon"]'])
def test_broken_managed_response_never_falls_back(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str,
) -> None:
    launcher_path(host)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output))
    with pytest.raises(OSError, match="Hermes runtime command"):
        runtime.hermes_command([], hermes_home=tmp_path)


@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, "hermes"), subprocess.TimeoutExpired("hermes", 10)])
def test_failed_managed_resolution_never_falls_back(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    launcher_path(host)

    def run(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(runtime.subprocess, "run", run)
    with pytest.raises(OSError, match="Could not resolve"):
        runtime.hermes_command([], hermes_home=tmp_path)


@pytest.mark.parametrize("isolated", [False, True])
def test_missing_managed_launcher_is_explicit_failure(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated: bool,
) -> None:
    if isolated:
        monkeypatch.setattr(runtime.sys, "flags", SimpleNamespace(isolated=1))
    else:
        (host / ".hermes" / "bin").mkdir(parents=True)
    with pytest.raises(OSError, match="launcher missing"):
        runtime.hermes_command([], hermes_home=tmp_path)


@pytest.mark.parametrize("site", ["okf", "hooks", "supervisor"])
def test_all_worker_launch_sites_use_managed_command(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: str,
) -> None:
    launcher_path(host)
    profile = tmp_path / "profile"
    profile.mkdir()
    cfg = resolve_config(profile)
    prefix = ["/store/python", "-I", "-c", "import hermes_bootstrap"]
    commands: list[list[str]] = []
    options: list[dict[str, Any]] = []

    def run(command: list[str], **kwargs: Any) -> Any:
        return SimpleNamespace(stdout=json.dumps(prefix + command[3:]))

    class Child:
        pid = 123

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            commands.append(command)
            options.append(kwargs)

        def wait(self, **kwargs: Any) -> int:
            return 0

        def __enter__(self) -> Child:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Child)
    monkeypatch.setattr(okf, "_start_worker_reaper", lambda process: None)
    monkeypatch.setattr(shadow_hooks, "_start_worker_reaper", lambda process: None)
    if site == "okf":
        assert okf._spawn_worker(cfg)
        role = "okf-worker"
    elif site == "hooks":
        assert shadow_hooks._spawn_worker(cfg)
        role = "routing-supervisor"
    else:
        monkeypatch.setattr(shadow_supervisor, "resolve_config", lambda home: cfg)
        monkeypatch.setattr(shadow_supervisor.shadow, "_enabled", lambda cfg: True)
        delays = iter([0, None, None])
        monkeypatch.setattr(shadow_supervisor, "work_delay", lambda cfg: next(delays))
        with sqlite3.connect(":memory:") as gate:
            gate.execute("CREATE TABLE wake (id INTEGER PRIMARY KEY, finished_ns INTEGER NOT NULL)")
            assert shadow_supervisor._run(cfg, time.monotonic() + 10, gate) == "idle"
        role = "routing-worker"
    expected = prefix + ["local-knowledge", role, "--hermes-home", str(cfg.hermes_home)]
    assert len(commands) == 1
    if site == "hooks":
        assert commands[0][:-2] == expected
        assert commands[0][-2] == "--wake-ns"
    else:
        assert commands[0] == expected
    assert options[0]["env"]["HERMES_HOME"] == str(cfg.hermes_home)
    assert options[0]["env"][okf.OKF_WORKER_ENV] == "1"
    assert options[0]["cwd"] == str(cfg.hermes_home)
    assert options[0].get("shell", False) is False


@pytest.mark.skipif(os.name == "nt", reason="Disposable POSIX launcher fixture")
def test_managed_subprocess_bootstraps_source_without_pythonpath(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The source exists only in the parent's import path. Raw store Python -m
    # cannot find it; the exact host launcher supplies bootstrap at child start.
    (host / "hermes_bootstrap.py").write_text("import os\nos.environ['BOOTSTRAPPED'] = 'yes'\n")
    (host / "hermes_cli" / "main.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps([sys.flags.isolated, os.environ['BOOTSTRAPPED'], "
        "os.environ['HERMES_HOME'], sys.argv[1:]]))\n")
    code = (
        f"import sys, runpy; sys.path.insert(0, {str(host)!r}); "
        "import hermes_bootstrap; runpy.run_module('hermes_cli.main', run_name='__main__')"
    )
    prefix = [sys.executable, "-I", "-S", "-c", code]
    printer = "import json, sys; print(json.dumps(" + repr(prefix) + " + sys.argv[3:]))"
    launcher = launcher_path(host)
    launcher.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", "-c", printer]) + ' "$@"\n')
    launcher.chmod(0o755)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "unrelated-bin"))
    raw = subprocess.run([sys.executable, "-I", "-S", "-m", "hermes_cli.main"],
                         cwd=tmp_path, capture_output=True, text=True)
    assert raw.returncode != 0
    args = ["local-knowledge", "okf-worker"]
    command = runtime.hermes_command(args, hermes_home=tmp_path)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    child = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert json.loads(child.stdout) == [1, "yes", str(tmp_path), args]
