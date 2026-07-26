"""Detached, bounded worker for automatic tool OKF generation."""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from . import okf
from .hooks import _fail_claimed_rows, _generate_claimed_okfs
from .runtime import _runtime_config

logger = logging.getLogger(__name__)


def _lease_heartbeat_interval(lease_seconds: int) -> float:
    return min(30.0, max(1.0, lease_seconds / 3))


def _start_lease_heartbeat(
    *,
    state_dir: Path,
    owner: str,
    lease_seconds: int,
) -> tuple[threading.Event, threading.Event, threading.Thread]:
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat() -> None:
        interval = _lease_heartbeat_interval(lease_seconds)
        while not stop.wait(interval):
            try:
                if not okf.renew_generation_lease(
                    state_dir,
                    owner=owner,
                    lease_seconds=lease_seconds,
                ):
                    logger.error("Local-knowledge OKF worker lost its generation lease")
                    lost.set()
                    return
            except Exception:
                logger.warning("Failed to renew local-knowledge OKF generation lease", exc_info=True)

    thread = threading.Thread(
        target=heartbeat,
        name=f"local-knowledge-okf-lease-{owner[:8]}",
        daemon=True,
    )
    thread.start()
    return stop, lost, thread


def _verify_generation_lease(
    *,
    state_dir: Path,
    owner: str,
    lease_seconds: int,
    lost: threading.Event,
) -> bool:
    """Fence publication by renewing the worker's still-owned lease."""

    if lost.is_set():
        return False
    try:
        current = okf.renew_generation_lease(
            state_dir,
            owner=owner,
            lease_seconds=lease_seconds,
        )
    except Exception:
        logger.warning("Failed to verify local-knowledge OKF generation lease", exc_info=True)
        return False
    if not current:
        lost.set()
    return current


def run_worker(*, llm: Any, hermes_home: Path | str | None = None) -> int:
    """Drain one bounded OKF batch through this process's host-owned LLM."""

    cfg = _runtime_config(hermes_home)
    if not cfg.okf.enabled or not cfg.okf.auto_generate:
        return 0
    if llm is None:
        logger.error("Local-knowledge OKF worker started without a host-owned ctx.llm facade")
        return 2

    stale_after = max(cfg.okf.max_generation_seconds * 2, 60)
    owner = uuid.uuid4().hex
    if not okf.acquire_generation_lease(
        cfg.state_dir,
        owner=owner,
        lease_seconds=stale_after,
    ):
        return 0

    claimed: list[dict[str, Any]] = []
    heartbeat_stop: threading.Event | None = None
    heartbeat_lost: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    try:
        heartbeat_stop, heartbeat_lost, heartbeat_thread = _start_lease_heartbeat(
            state_dir=cfg.state_dir,
            owner=owner,
            lease_seconds=stale_after,
        )
        okf.recover_stale_claims(
            cfg.state_dir,
            stale_after_seconds=stale_after,
            max_attempts=okf.DEFAULT_MAX_ATTEMPTS,
        )
        claimed = okf.claim_candidates(
            cfg.state_dir,
            limit=cfg.okf.max_candidates_per_session,
            min_use_count=cfg.okf.min_use_count,
            stale_after_seconds=stale_after,
        )
        if not claimed:
            return 0
        assert heartbeat_lost is not None
        return (
            0
            if _generate_claimed_okfs(
                cfg,
                llm=llm,
                rows=claimed,
                lease_owner=owner,
                can_publish=lambda: _verify_generation_lease(
                    state_dir=cfg.state_dir,
                    owner=owner,
                    lease_seconds=stale_after,
                    lost=heartbeat_lost,
                ),
            )
            else 1
        )
    except Exception:
        if claimed:
            _fail_claimed_rows(cfg, claimed, error="host LLM generation failed")
        logger.exception("Failed to generate local-knowledge OKFs in detached worker")
        return 1
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        if not okf.release_generation_lease(cfg.state_dir, owner=owner):
            logger.warning("Local-knowledge OKF worker no longer owned its generation lease at exit")
