"""Fail-open host adapters for opt-in private routing observations.

Hermes runs hooks in copied contexts on worker threads. A bounded locked map,
not a ContextVar or a latest-request fallback, joins exact host turn identities.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, resolve_config
from .implicit import _hook_succeeded, _usage_event_id
from .okf import OKF_WORKER_ENV, _detached_process_kwargs, _start_worker_reaper

logger = logging.getLogger(__name__)
SHADOW_WORKER_ENV = "LOCAL_KNOWLEDGE_SHADOW_WORKER"
MAX_USER_REQUEST_CHARS = 4000
MAX_CONTEXTS = 128
CONTEXT_TTL_SECONDS = 15 * 60
_ContextKey = tuple[Path, Path, Path, str, str, str]


@dataclass
class _Request:
    expires: float
    text: str
    receipts: set[int] = field(default_factory=set)


_REQUESTS: dict[_ContextKey, _Request] = {}
_LOCK = threading.Lock()


def _identity(value: Any) -> str:
    # Reject rather than truncate IDs: common prefixes are not the same turn.
    return value if isinstance(value, str) and value.strip() and len(value) <= 128 else ""


def _key(cfg: Config, context: Mapping[str, Any]) -> _ContextKey | None:
    session, task, turn = (_identity(context.get(key)) for key in (
        "session_id", "task_id", "turn_id",
    ))
    if not session or not task or not turn:
        return None
    return cfg.hermes_home, cfg.source_root, cfg.state_dir, session, task, turn


def _prune(now: float) -> None:
    for key, request in list(_REQUESTS.items()):
        if request.expires <= now:
            del _REQUESTS[key]


def _clear_profile(cfg: Config) -> None:
    with _LOCK:
        for key in list(_REQUESTS):
            if key[0] == cfg.hermes_home:
                del _REQUESTS[key]


def _excluded(context: Mapping[str, Any]) -> bool:
    return bool(
        context.get("parent_session_id")
        or context.get("worker_generated")
        or any(
            os.environ.get(key, "").strip().lower() in {"1", "true", "yes", "on"}
            for key in (SHADOW_WORKER_ENV, OKF_WORKER_ENV)
        )
    )


def on_pre_llm_call(**kwargs: Any) -> None:
    """Bind only an exact bounded current message; never read history."""
    try:
        cfg = resolve_config()
        if cfg.verified_routing.mode != "shadow":
            _clear_profile(cfg)
            return
        key = _key(cfg, kwargs)
        now = time.monotonic()
        with _LOCK:
            _prune(now)
            if key is None:
                return
            _REQUESTS.pop(key, None)
            request = kwargs.get("user_message")
            if (
                _excluded(kwargs) or not isinstance(request, str) or not request.strip()
                or len(request) > MAX_USER_REQUEST_CHARS
            ):
                return
            while len(_REQUESTS) >= MAX_CONTEXTS:
                del _REQUESTS[next(iter(_REQUESTS))]
            _REQUESTS[key] = _Request(now + CONTEXT_TTL_SECONDS, request)
    except Exception:
        logger.debug("Shadow context binding skipped")


def on_post_tool_call(**kwargs: Any) -> None:
    """Join a successful search receipt to its exact hook context once."""
    try:
        if kwargs.get("tool_name") != "knowledge_search" or _excluded(kwargs):
            return
        cfg = resolve_config()
        if cfg.verified_routing.mode != "shadow":
            _clear_profile(cfg)
            return
        if not _hook_succeeded(kwargs) or not _identity(kwargs.get("api_request_id")):
            return
        key = _key(cfg, kwargs)
        with _LOCK:
            _prune(time.monotonic())
            bound = _REQUESTS.get(key) if key is not None else None
        if bound is None or key is None:
            return
        event_id = _usage_event_id(kwargs.get("result"))
        if event_id is None:
            return
        # Handler bridges omit turn/API IDs. The host post hook supplies them;
        # its result receipt links to the handler's authoritative baseline row.
        path = cfg.state_dir / "usage.sqlite"
        if not path.is_file():
            return
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)
        try:
            row = conn.execute(
                "SELECT query, artifact_type, baseline_top_ids_json, turn_id, api_request_id FROM usage_events "
                "WHERE id=? AND session_id=? AND task_id=? AND root=? "
                "AND success=1 AND client='native' AND tool='knowledge_search'",
                (event_id, key[3], key[4], str(cfg.source_root)),
            ).fetchone()
        finally:
            conn.close()
        args = kwargs.get("args")
        if row is None or not isinstance(args, Mapping):
            return
        query, artifact_type, raw_baseline, row_turn, row_api = row
        if (
            query != str(args.get("query") or "").strip()
            or (artifact_type or "") != str(args.get("artifact_type") or "").strip()
            or (row_turn and row_turn != key[5])
            or (row_api and row_api != kwargs.get("api_request_id"))
        ):
            return
        baseline_ids = json.loads(raw_baseline)
        if (
            not isinstance(baseline_ids, list) or len(baseline_ids) > 30
            or not all(isinstance(item, str) for item in baseline_ids)
        ):
            return
        from . import shadow

        with _LOCK:
            if (
                _REQUESTS.get(key) is not bound or bound.expires <= time.monotonic()
                or event_id in bound.receipts or len(bound.receipts) >= 128
            ):
                return
            bound.receipts.add(event_id)
        shadow.observe(
            cfg, user_request=bound.text, query=query, artifact_type=artifact_type or "",
            session_id=key[3], task_id=key[4], turn_id=key[5], baseline_ids=baseline_ids,
            lookup=args.get("lookup"),
        )
    except Exception:
        # Exceptions may include private task/source text; never log their value.
        logger.debug("Shadow search observation skipped")


def on_session_end(**kwargs: Any) -> None:
    """Release this exact turn's text and mark this profile's session work ready."""
    try:
        cfg = resolve_config()
        key = _key(cfg, kwargs)
        with _LOCK:
            _prune(time.monotonic())
            if key is not None:
                _REQUESTS.pop(key, None)
        if cfg.verified_routing.mode != "shadow":
            _clear_profile(cfg)
            return
        session_id = _identity(kwargs.get("session_id"))
        if _excluded(kwargs) or not session_id:
            return
        from . import shadow

        shadow.finish_session(cfg, session_id)
        _wake_supervisor(cfg)
    except Exception:
        logger.debug("Shadow session closure skipped")


def _spawn_worker(cfg: Config) -> bool:
    from .shadow_supervisor import worker_env

    env = worker_env(cfg)
    process = subprocess.Popen(
        [sys.executable, "-m", "hermes_cli.main", "local-knowledge", "routing-supervisor",
         "--hermes-home", str(cfg.hermes_home), "--wake-ns", str(time.time_ns())],
        cwd=str(cfg.hermes_home),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **_detached_process_kwargs(),
    )
    _start_worker_reaper(process)
    # Fixed-size structural status only: host/provider stdout may contain text.
    try:
        (cfg.state_dir / "routing_worker.log").write_text("Worker launched.\n", encoding="utf-8")
    except OSError:
        pass
    return True


def _wake_supervisor(cfg: Config) -> bool:
    from .shadow_supervisor import work_delay

    return _spawn_worker(cfg) if work_delay(cfg) is not None else False


def on_session_finalize(**kwargs: Any) -> bool:
    """Read-only ready-work check plus detached launch; never run inference here."""
    try:
        if _excluded(kwargs):
            return False
        cfg = resolve_config()
        if cfg.verified_routing.mode != "shadow":
            _clear_profile(cfg)
            return False
        return _wake_supervisor(cfg)
    except Exception:
        logger.debug("Shadow worker launch skipped")
        return False
