"""Plugin lifecycle hooks for opportunistic tool OKF generation."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import okf
from .runtime import RuntimeConfig, _runtime_config

logger = logging.getLogger(__name__)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _inside_okf_worker() -> bool:
    return _truthy_env(okf.OKF_WORKER_ENV)


def _tool_metadata(tool_name: str) -> tuple[str | None, dict[str, Any] | None]:
    try:
        from tools.registry import registry  # type: ignore

        schema = registry.get_schema(tool_name)
        toolset = registry.get_toolset_for_tool(tool_name)
        return toolset, schema
    except Exception:
        logger.debug("Could not inspect tool registry metadata for %s", tool_name, exc_info=True)
        return None, None


def _classify_result(result: Any) -> tuple[bool, str | None, str | None]:
    if not isinstance(result, str):
        return True, None, None
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return True, None, None
    if not isinstance(parsed, dict):
        return True, None, None
    if parsed.get("success") is False or "error" in parsed:
        error = parsed.get("error") or parsed.get("message") or "tool_error"
        return False, "tool_error", str(error)
    return True, None, None


def _on_post_tool_call(**kwargs: Any) -> None:
    if _inside_okf_worker():
        return
    try:
        cfg = _runtime_config()
        if not cfg.okf.enabled:
            return
        tool_name = kwargs.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return
        args = kwargs.get("args")
        if not isinstance(args, dict):
            args = {}
        success, error_type, error_message = _classify_result(kwargs.get("result"))
        toolset, schema = _tool_metadata(tool_name)
        okf.upsert_tool_candidate(
            cfg.state_dir,
            tool_name=tool_name,
            toolset=toolset,
            schema=schema,
            args=args,
            success=success,
            error_type=error_type,
            error_message=error_message,
        )
    except Exception:
        logger.exception("Failed to record local-knowledge OKF tool candidate")


def _lock_payload() -> str:
    payload = {"pid": os.getpid(), "created_at": time.time()}
    return json.dumps(payload, sort_keys=True)


def _lock_is_stale(lock_path: Path, *, stale_after_seconds: int) -> bool:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        created_at = float(payload.get("created_at", 0))
    except Exception:
        return True
    return time.time() - created_at > stale_after_seconds


def _acquire_worker_lock(lock_path: Path, *, stale_after_seconds: int) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not _lock_is_stale(lock_path, stale_after_seconds=stale_after_seconds):
            return False
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        return _acquire_worker_lock(lock_path, stale_after_seconds=stale_after_seconds)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(_lock_payload())
    return True


def _release_worker_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def build_worker_prompt(cfg: RuntimeConfig) -> str:
    limit = cfg.okf.max_candidates_per_session
    return f"""You are the bounded OKF worker for hermes-local-knowledge.

Run exactly this protocol and then exit:
1. Claim at most {limit} candidates:
   python -m hermes_local_knowledge.cli okf claim --from-hermes-config --limit {limit} --json
2. For each claimed candidate, write exactly one compact Markdown OKF file at the provided path.
3. Use only the candidate packet fields: tool name, toolset, schema JSON, schema hash, counters, and argument shape.
4. Do not inspect raw session transcripts, raw tool outputs, emails, OCR text, or private documents.
5. Validate each file:
   python -m hermes_local_knowledge.cli okf validate --from-hermes-config --claim-token <token> --path <path> --json
6. If validation passes, mark it complete:
   python -m hermes_local_knowledge.cli okf complete --from-hermes-config --claim-token <token> --tool <tool> --path <path> --json
7. If generation or validation fails, mark it failed:
   python -m hermes_local_knowledge.cli okf fail --from-hermes-config --claim-token <token> --tool <tool> --error <short-redacted-error> --json
8. Stop after at most {limit} candidates. Do not schedule cron jobs or spawn more workers.
""".strip()


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _prepend_pythonpath(env: dict[str, str], path: Path) -> None:
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(path) if not existing else f"{path}{os.pathsep}{existing}"


def build_worker_command(cfg: RuntimeConfig, *, lock_path: Path | None = None) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "hermes_local_knowledge.okf_worker",
        "--timeout",
        str(cfg.okf.max_worker_seconds),
        "--toolsets",
        ",".join(cfg.okf.worker_toolsets),
        "--source",
        cfg.okf.worker_source,
        "--prompt",
        build_worker_prompt(cfg),
    ]
    if lock_path is not None:
        command.extend(["--lock-path", str(lock_path)])
    return command


def _spawn_worker(cfg: RuntimeConfig) -> bool:
    log_path = cfg.state_dir / "okf_worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env[okf.OKF_WORKER_ENV] = "1"
    env["HERMES_HOME"] = str(cfg.hermes_home)
    _prepend_pythonpath(env, _package_root())
    lock_path = okf.worker_lock_path(cfg.state_dir)
    command = build_worker_command(cfg, lock_path=lock_path)
    cwd = cfg.source_root if cfg.source_root.exists() else cfg.hermes_home
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        return True
    finally:
        log_handle.close()


def _on_session_end(**kwargs: Any) -> bool:
    if _inside_okf_worker():
        return False
    try:
        cfg = _runtime_config()
        if not cfg.okf.enabled or not cfg.okf.auto_generate:
            return False
        if not okf.pending_candidates(cfg.state_dir, limit=1, min_use_count=cfg.okf.min_use_count):
            return False
        lock_path = okf.worker_lock_path(cfg.state_dir)
        stale_after = max(cfg.okf.max_worker_seconds * 2, 60)
        if not _acquire_worker_lock(lock_path, stale_after_seconds=stale_after):
            return False
        try:
            return _spawn_worker(cfg)
        except Exception:
            _release_worker_lock(lock_path)
            logger.exception("Failed to spawn local-knowledge OKF worker")
            return False
    except Exception:
        logger.exception("Failed during local-knowledge OKF session-end hook")
        return False
