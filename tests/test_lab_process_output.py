"""Harmless local producers exercise private output storage, never candidate code."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tracemalloc

import pytest

from scripts import lab_process_output as output


@pytest.mark.parametrize("kind", ["finite", "infinite", "exact", "zero_exit_overflow"])
def test_bounded_process_real_pipes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    ceiling = 1024 * 1024
    monkeypatch.setattr(output, "COMMAND_OUTPUT_BYTES", ceiling)
    code = {
        "finite": "import os; os.write(1,b'x'*500000); os.write(2,b'e'*200000); raise SystemExit(7)",
        "infinite": "import os\nwhile True: os.write(1,b'x'*8192); os.write(2,b'e'*8192)",
        "exact": f"import os; os.write(1,b'x'*{ceiling}); os.write(2,b'')",
        "zero_exit_overflow": f"import os; os.write(1,b'x'*{ceiling + 1})",
    }[kind]
    stopped = []
    prefix = tmp_path / "producer"
    tracemalloc.start()
    try:
        result = output.bounded_process([sys.executable, "-c", code], prefix=prefix,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, on_limit=lambda: stopped.append(True))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024
    exceeded = kind in {"infinite", "zero_exit_overflow"}
    assert bool(stopped) is exceeded
    assert result.receipt["output_limit_exceeded"] is exceeded
    assert result.returncode != 0 if exceeded else result.returncode == (7 if kind == "finite" else 0)
    assert len(result.stdout) <= output.PREVIEW_BYTES and len(result.stderr) <= output.PREVIEW_BYTES
    receipt = json.loads(prefix.with_suffix(".output.json").read_text())
    assert receipt == result.receipt
    assert sum(receipt["stored_bytes"].values()) == (700000 if kind == "finite" else ceiling)
    assert stat.S_IMODE(prefix.with_suffix(".output.json").stat().st_mode) == 0o600
    for key in ("stdout", "stderr"):
        path = tmp_path / receipt["files"][key]
        assert path.stat().st_size == receipt["stored_bytes"][key]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    if kind == "finite":
        assert prefix.with_suffix(".stdout").read_bytes() == b'x' * 500000
        assert prefix.with_suffix(".stderr").read_bytes() == b'e' * 200000


def test_failed_stop_is_durable_and_never_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output, "COMMAND_OUTPUT_BYTES", 100)
    children = []
    popen = output.subprocess.Popen
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(output.subprocess, "Popen", spawn)
    def refuse() -> None:
        raise ValueError("unowned worker")
    with pytest.raises(ValueError, match="unowned"):
        output.bounded_process([sys.executable, "-c", "import os\nwhile True: os.write(1,b'x'*8192)"],
                               prefix=tmp_path / "failed", env={}, on_limit=refuse)
    assert children[0].poll() is not None
    receipt = json.loads((tmp_path / "failed.output.json").read_text())
    assert receipt["output_limit_exceeded"] and not receipt["limit_action_complete"]
    assert receipt["error_class"] == "ValueError"


def test_private_receipt_fsyncs_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fsync = os.fsync
    modes = []
    def record(fd: int) -> None:
        modes.append(os.fstat(fd).st_mode)
        fsync(fd)
    monkeypatch.setattr(output.os, "fsync", record)
    output.save_receipt(tmp_path / "receipt.json", {"synthetic": True})
    assert any(stat.S_ISREG(mode) for mode in modes)
    assert stat.S_ISDIR(modes[-1])
