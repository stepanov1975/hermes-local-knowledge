"""Plugin lifecycle hooks for opportunistic tool OKF generation."""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import okf
from .runtime import RuntimeConfig, _runtime_config

logger = logging.getLogger(__name__)

OKF_GENERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "okfs": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "schema_hash": {"type": "string"},
                    "title": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "triggers": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "when_not_to_use": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "related_tools": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "body": {"type": "string"},
                },
                "required": [
                    "tool",
                    "schema_hash",
                    "title",
                    "aliases",
                    "triggers",
                    "when_not_to_use",
                    "related_tools",
                    "body",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["okfs"],
    "additionalProperties": False,
}


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
    if parsed.get("success") is False or bool(parsed.get("error")):
        error = parsed.get("error") or parsed.get("message") or "tool_error"
        return False, "tool_error", str(error)
    return True, None, None


def _classify_hook_outcome(kwargs: Mapping[str, Any]) -> tuple[bool, str | None, str | None]:
    status = kwargs.get("status")
    if isinstance(status, str) and status.strip():
        normalized = status.strip().lower()
        if normalized in {"ok", "success"}:
            return True, None, None
        error_type = kwargs.get("error_type") or normalized
        error_message = kwargs.get("error_message") or kwargs.get("result") or normalized
        return False, str(error_type), str(error_message)
    return _classify_result(kwargs.get("result"))


def _on_post_tool_call(**kwargs: Any) -> None:
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
        success, error_type, error_message = _classify_hook_outcome(kwargs)
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


def _acquire_generation_lock(lock_path: Path, *, stale_after_seconds: int) -> bool:
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
        return _acquire_generation_lock(lock_path, stale_after_seconds=stale_after_seconds)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(_lock_payload())
    return True


def _release_generation_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _generation_packet(row: Mapping[str, Any], state_dir: Path) -> dict[str, Any]:
    packet = okf.candidate_packet(row, state_dir)
    return {
        key: packet[key]
        for key in (
            "tool",
            "toolset",
            "schema_hash",
            "schema",
            "arg_shape",
            "use_count",
            "success_count",
            "error_count",
            "last_error_type",
        )
    }


def _bounded_list(value: Any, *, limit: int = 8, max_chars: int = 240) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).replace("\x00", "").strip()[:max_chars] for item in value[:limit] if str(item).strip()]


def _quoted(value: Any, *, max_chars: int = 500) -> str:
    clean = str(value).replace("\x00", "").strip()[:max_chars]
    return json.dumps(clean, ensure_ascii=False)


def _render_okf(item: Mapping[str, Any]) -> str:
    tool_name = str(item.get("tool") or "").strip()
    schema_digest = str(item.get("schema_hash") or "").strip()
    title = str(item.get("title") or f"Tool OKF: {tool_name}").strip()[:500]
    body = str(item.get("body") or "").replace("\x00", "").strip()[:4_000]
    lines = [
        "---",
        "artifact_type: tool_okf",
        f"tool: {_quoted(tool_name)}",
        f"schema_hash: {_quoted(schema_digest)}",
        f"title: {_quoted(title)}",
        f"generated_at: {_quoted(okf.utc_now())}",
    ]
    for key in ("aliases", "triggers", "when_not_to_use", "related_tools"):
        lines.append(f"{key}:")
        values = _bounded_list(item.get(key))
        lines.extend(f"  - {_quoted(value, max_chars=240)}" for value in values)
    lines.extend(["---", "", f"# {title}", "", body, ""])
    return "\n".join(lines)


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.write_bytes(previous)


def _write_and_complete_item(
    cfg: RuntimeConfig,
    *,
    row: Mapping[str, Any],
    item: Mapping[str, Any],
) -> bool:
    tool_name = str(row.get("tool_name") or "")
    claim_token = str(row.get("claim_token") or "")
    if item.get("tool") != tool_name or item.get("schema_hash") != row.get("schema_hash"):
        okf.mark_candidate_error(
            cfg.state_dir,
            tool_name=tool_name,
            claim_token=claim_token,
            error="generated identity mismatch",
        )
        return False
    path = okf.okf_file_path(cfg.state_dir, tool_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_bytes() if path.exists() else None
    temp_path = path.with_suffix(".md.tmp")
    temp_path.write_text(_render_okf(item), encoding="utf-8")
    os.replace(temp_path, path)
    validation = okf.validate_okf_file(cfg.state_dir, claim_token=claim_token, path=path)
    if not validation["valid"]:
        _restore_file(path, previous)
        okf.mark_candidate_error(
            cfg.state_dir,
            tool_name=tool_name,
            claim_token=claim_token,
            error="generated validation failed",
        )
        return False
    if okf.mark_candidate_done(
        cfg.state_dir,
        tool_name=tool_name,
        claim_token=claim_token,
        okf_path=path,
    ):
        return True
    _restore_file(path, previous)
    return False


def _fail_claimed_rows(cfg: RuntimeConfig, rows: list[dict[str, Any]], *, error: str) -> None:
    for row in rows:
        okf.mark_candidate_error(
            cfg.state_dir,
            tool_name=str(row.get("tool_name") or ""),
            claim_token=str(row.get("claim_token") or ""),
            error=error,
        )


def _generate_claimed_okfs(cfg: RuntimeConfig, *, llm: Any, rows: list[dict[str, Any]]) -> bool:
    packets = [_generation_packet(row, cfg.state_dir) for row in rows]
    result = llm.complete_structured(
        instructions=(
            "Create one compact routing note for every supplied Hermes tool candidate. "
            "Use only the supplied structural packet. Never infer or request raw transcripts, "
            "tool outputs, document contents, emails, credentials, or secret values. Return every "
            "tool and schema_hash exactly as supplied. Aliases and triggers must be specific multi-word "
            "phrases that help route user intent."
        ),
        input=[{"type": "text", "text": json.dumps({"candidates": packets}, ensure_ascii=False, sort_keys=True)}],
        json_schema=OKF_GENERATION_SCHEMA,
        schema_name="local_knowledge_tool_okfs",
        temperature=0.0,
        max_tokens=min(4_000, max(800, len(rows) * 1_000)),
        timeout=cfg.okf.max_generation_seconds,
        purpose="local_knowledge.okf_generation",
    )
    parsed = getattr(result, "parsed", None)
    items = parsed.get("okfs") if isinstance(parsed, Mapping) else None
    if not isinstance(items, list):
        _fail_claimed_rows(cfg, rows, error="structured response missing okfs")
        return False
    by_tool = {
        str(item.get("tool")): item
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("tool"), str)
    }
    completed = 0
    for row in rows:
        tool_name = str(row.get("tool_name") or "")
        item = by_tool.get(tool_name)
        if item is None:
            okf.mark_candidate_error(
                cfg.state_dir,
                tool_name=tool_name,
                claim_token=str(row.get("claim_token") or ""),
                error="structured response omitted candidate",
            )
            continue
        completed += int(_write_and_complete_item(cfg, row=row, item=item))
    return completed > 0


def _on_session_finalize(*, llm: Any = None, **kwargs: Any) -> bool:
    try:
        cfg = _runtime_config()
        if not cfg.okf.enabled or not cfg.okf.auto_generate or llm is None:
            return False
        stale_after = max(cfg.okf.max_generation_seconds * 2, 60)
        okf.recover_stale_claims(
            cfg.state_dir,
            stale_after_seconds=stale_after,
            max_attempts=okf.DEFAULT_MAX_ATTEMPTS,
        )
        if not okf.pending_candidates(cfg.state_dir, limit=1, min_use_count=cfg.okf.min_use_count):
            return False
        lock_path = okf.generation_lock_path(cfg.state_dir)
        if not _acquire_generation_lock(lock_path, stale_after_seconds=stale_after):
            return False
        try:
            claimed = okf.claim_candidates(
                cfg.state_dir,
                limit=cfg.okf.max_candidates_per_session,
                min_use_count=cfg.okf.min_use_count,
                stale_after_seconds=stale_after,
            )
            if not claimed:
                return False
            try:
                return _generate_claimed_okfs(cfg, llm=llm, rows=claimed)
            except Exception:
                _fail_claimed_rows(cfg, claimed, error="host LLM generation failed")
                logger.exception("Failed to generate local-knowledge OKFs through ctx.llm")
                return False
        finally:
            _release_generation_lock(lock_path)
    except Exception:
        logger.exception("Failed during local-knowledge OKF session-finalize hook")
        return False


def _on_session_end(**kwargs: Any) -> bool:
    """Backward-compatible import alias; Hermes registration uses finalization."""

    return _on_session_finalize(**kwargs)
