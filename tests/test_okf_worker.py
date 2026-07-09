from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from hermes_local_knowledge import okf_worker


def test_okf_worker_wraps_hermes_chat_with_timeout_and_releases_lock(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lock_path = tmp_path / "worker.lock"
    lock_path.write_text("locked", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class Result:
        returncode = 7

    def fake_run(command, *, timeout, check):  # type: ignore[no-untyped-def]
        calls.append({"command": command, "timeout": timeout, "check": check})
        return Result()

    monkeypatch.setattr(okf_worker.subprocess, "run", fake_run)

    rc = okf_worker.main(
        [
            "--timeout",
            "42",
            "--toolsets",
            "terminal,file",
            "--source",
            "okf-test",
            "--prompt",
            "do work",
            "--lock-path",
            str(lock_path),
        ]
    )

    assert rc == 7
    assert len(calls) == 1
    assert calls[0]["timeout"] == 42
    assert calls[0]["check"] is False
    assert calls[0]["command"] == [
        "hermes",
        "chat",
        "-Q",
        "--toolsets",
        "terminal,file",
        "--source",
        "okf-test",
        "--max-turns",
        "20",
        "-q",
        "do work",
    ]
    assert not lock_path.exists()


def test_okf_worker_timeout_returns_124_and_releases_lock(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    lock_path = tmp_path / "worker.lock"
    lock_path.write_text("locked", encoding="utf-8")

    def fake_run(command, *, timeout, check):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(okf_worker.subprocess, "run", fake_run)

    rc = okf_worker.main(
        [
            "--timeout",
            "1",
            "--toolsets",
            "terminal,file",
            "--source",
            "okf-test",
            "--prompt",
            "do work",
            "--lock-path",
            str(lock_path),
        ]
    )

    assert rc == 124
    assert "timed out after 1s" in capsys.readouterr().err
    assert not lock_path.exists()
