"""Private bounded output for candidate-dependent Docker commands, without deadlines."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
from typing import Any, Callable

from scripts import lab_disk_guard as disk
from scripts import lab_recovery as recovery

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


class NativeStderr:
    """Drain transport stderr independently of Frames and trusted-test waits."""

    def __init__(self, process: subprocess.Popen, path: Path, on_limit: Callable[[], None]):
        self.process = process
        self.path = path
        self.on_limit = on_limit
        self.stopping = threading.Event()
        self.error: BaseException | None = None
        self.exceeded = False
        self.finished = False
        self.stored = 0
        self.limit_action_complete = False
        self.thread = threading.Thread(target=self._drain, name="lab-native-stderr")
        self.stream = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb")
        self.thread.start()

    def _drain(self) -> None:
        assert self.process.stderr is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stderr, selectors.EVENT_READ)
                while not self.stopping.is_set():
                    if not selector.select(0.1):
                        continue
                    chunk = os.read(self.process.stderr.fileno(), 65536)
                    if not chunk:
                        return
                    remaining = COMMAND_OUTPUT_BYTES - self.stored
                    self.stream.write(chunk[:remaining])
                    self.stored += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        self.exceeded = True
                        self.on_limit()
                        self.limit_action_complete = True
                        raise RuntimeError("Native stderr storage ceiling exceeded")
        except BaseException as error:
            self.error = error
            # Wake a quiet Frames read even if Docker stop/readback failed.
            if self.process.poll() is None:
                self.process.kill()
        finally:
            try:
                self.stream.flush()
                os.fsync(self.stream.fileno())
                self.stream.close()
            except OSError as error:
                self.error = self.error or error
                if self.process.poll() is None:
                    self.process.kill()
                self.stream.close()

    def finish(self, *, abort: bool = False) -> None:
        if self.finished:
            return
        self.finished = True
        if abort:
            self.stopping.set()
        self.thread.join()
        assert self.process.stderr is not None
        self.process.stderr.close()
        save_receipt(self.path.with_suffix(".stderr.json"), {
            "stored_bytes": self.stored, "storage_ceiling_bytes": COMMAND_OUTPUT_BYTES,
            "output_limit_exceeded": self.exceeded,
            "limit_action_complete": self.limit_action_complete,
            "error_class": type(self.error).__name__ if self.error else None})
        if self.error is not None:
            raise RuntimeError("Native stderr capture failed") from self.error


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
        with disk.action():
            process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       pass_fds=recovery.lock_fds())
        assert process.stdout is not None and process.stderr is not None
        for key, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            stack.enter_context(pipe)
            selector.register(pipe, selectors.EVENT_READ, key)
        try:
            while selector.get_map() and not exceeded:
                disk.checkpoint()
                for selected, _ in selector.select(1):
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
                while True:
                    disk.checkpoint()
                    try:
                        process.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        pass
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
