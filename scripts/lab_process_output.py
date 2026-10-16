"""Private bounded output for candidate-dependent Docker commands, without deadlines."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import selectors
import subprocess
from typing import Any, Callable

# Combined storage resource guard, not a time/token/session allowance.
COMMAND_OUTPUT_BYTES = 64 * 1024 * 1024
PREVIEW_BYTES = 8192


class ProcessOutput(subprocess.CompletedProcess[bytes]):
    def __init__(self, args: list[str], returncode: int, stdout: bytes, stderr: bytes,
                 receipt: dict[str, Any]):
        super().__init__(args, returncode, stdout, stderr)
        self.receipt = receipt


def save_receipt(path: Path, value: dict[str, Any]) -> None:
    """Persist immutable private metadata and its parent directory."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def bounded_process(args: list[str], *, prefix: Path, env: dict[str, str],
                    on_limit: Callable[[], None]) -> ProcessOutput:
    """Retain raw streams to a combined ceiling and only small previews in RAM.

    The caller must stop its exact owned Docker worker in on_limit: killing the
    attached Docker CLI alone cannot stop candidate execution. Callback failures
    propagate after durable diagnostics and local CLI reaping, never as success.
    Prefix and its parent must be controller-owned, outside candidate binds.
    """
    previews = {"stdout": bytearray(), "stderr": bytearray()}
    sizes = {"stdout": 0, "stderr": 0}
    paths = {key: prefix.with_suffix("." + key) for key in previews}
    exceeded = False
    limit_action_complete = False
    exit_code = None
    error_class = None
    receipt: dict[str, Any] = {}
    with ExitStack() as stack:
        files = {key: stack.enter_context(os.fdopen(os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"))
            for key, path in paths.items()}
        selector = stack.enter_context(selectors.DefaultSelector())
        process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None and process.stderr is not None
        for key, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            stack.enter_context(pipe)
            selector.register(pipe, selectors.EVENT_READ, key)
        try:
            while selector.get_map() and not exceeded:
                for selected, _ in selector.select():
                    chunk = os.read(selected.fd, 65536)
                    if not chunk:
                        selector.unregister(selected.fileobj)
                        continue
                    key = selected.data
                    remaining = COMMAND_OUTPUT_BYTES - sum(sizes.values())
                    stored = chunk[:remaining]
                    files[key].write(stored)
                    sizes[key] += len(stored)
                    previews[key].extend(stored[:max(0, PREVIEW_BYTES - len(previews[key]))])
                    if len(chunk) > remaining:
                        exceeded = True
                        on_limit()
                        limit_action_complete = True
                        break
            if not exceeded:
                process.wait()
        except BaseException as error:
            error_class = type(error).__name__
            raise
        finally:
            if process.poll() is None:
                process.kill()
            exit_code = process.wait()
            for stream in files.values():
                stream.flush()
                os.fsync(stream.fileno())
            receipt = {"args": args, "exit_code": exit_code,
                       "output_limit_exceeded": exceeded,
                       "limit_action_complete": limit_action_complete, "error_class": error_class,
                       "storage_ceiling_bytes": COMMAND_OUTPUT_BYTES,
                       "files": {key: path.name for key, path in paths.items()}, "stored_bytes": sizes,
                       **{key: value.decode("utf-8", "replace") for key, value in previews.items()}}
            save_receipt(prefix.with_suffix(".output.json"), receipt)
    return ProcessOutput(args, (exit_code or 1) if exceeded else exit_code,
                         bytes(previews["stdout"]), bytes(previews["stderr"]), receipt)
