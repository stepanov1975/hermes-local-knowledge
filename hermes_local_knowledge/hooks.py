"""Plugin lifecycle hooks for opportunistic tool OKF generation."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import okf
from .runtime import RuntimeConfig, _runtime_config

logger = logging.getLogger(__name__)
OKF_WORKER_ENV = "HERMES_LOCAL_KNOWLEDGE_OKF_WORKER"

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


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _inside_okf_worker() -> bool:
    return _truthy_env(OKF_WORKER_ENV)


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


def _worker_command(cfg: RuntimeConfig) -> list[str]:
    return [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "local-knowledge",
        "okf-worker",
        "--hermes-home",
        str(cfg.hermes_home),
    ]


def _detached_process_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        creationflags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
        return {"creationflags": creationflags}
    return {"start_new_session": True}


def _start_worker_reaper(process: subprocess.Popen[Any]) -> None:
    """Reap the detached child without making session finalization wait."""

    thread = threading.Thread(
        target=process.wait,
        name=f"local-knowledge-okf-worker-{process.pid}",
        daemon=True,
    )
    thread.start()


def _spawn_worker(cfg: RuntimeConfig) -> bool:
    log_path = cfg.state_dir / "okf_worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env[OKF_WORKER_ENV] = "1"
    env["HERMES_HOME"] = str(cfg.hermes_home)
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            _worker_command(cfg),
            cwd=str(cfg.hermes_home),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **_detached_process_kwargs(),
        )
        _start_worker_reaper(process)
        return True
    finally:
        log_handle.close()


def _generation_packet(row: Mapping[str, Any], state_dir: Path) -> dict[str, Any]:
    packet = okf.candidate_packet(row, state_dir)
    return {
        key: packet[key]
        for key in (
            "tool",
            "toolset",
            "schema_hash",
            "schema",
            "allowed_related_tools",
            "arg_shape",
        )
    }


def _bounded_list(value: Any, *, limit: int = 8, max_chars: int = 240) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).replace("\x00", "").strip()[:max_chars] for item in value[:limit] if str(item).strip()]


def _quoted(value: Any, *, max_chars: int = 500) -> str:
    clean = str(value).replace("\x00", "").strip()[:max_chars]
    return json.dumps(clean, ensure_ascii=False)


def _render_okf(item: Mapping[str, Any], *, toolset: str | None) -> str:
    tool_name = str(item.get("tool") or "").strip()
    schema_digest = str(item.get("schema_hash") or "").strip()
    title = str(item.get("title") or f"Tool OKF: {tool_name}").strip()[:500]
    body = str(item.get("body") or "").replace("\x00", "").strip()[:4_000]
    lines = [
        "---",
        "artifact_type: tool_okf",
        f"tool: {_quoted(tool_name)}",
    ]
    if toolset:
        lines.append(f"toolset: {_quoted(toolset)}")
    lines.extend(
        [
            f"schema_hash: {_quoted(schema_digest)}",
            f"generator_version: {_quoted(okf.OKF_GENERATOR_VERSION)}",
            f"title: {_quoted(title)}",
            f"generated_at: {_quoted(okf.utc_now())}",
        ]
    )
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
    lease_owner: str,
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
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(_render_okf(item, toolset=str(row.get("toolset") or "").strip() or None))
        temp_path = Path(handle.name)
    previous: bytes | None = None
    published = False

    def publish() -> None:
        nonlocal previous, published
        previous = path.read_bytes() if path.exists() else None
        os.replace(temp_path, path)
        published = True

    def rollback() -> None:
        if published:
            _restore_file(path, previous)

    try:
        prevalidation = okf.validate_okf_file(
            cfg.state_dir,
            claim_token=claim_token,
            path=path,
            _content_path=temp_path,
        )
        if not prevalidation["valid"]:
            okf.mark_candidate_error(
                cfg.state_dir,
                tool_name=tool_name,
                claim_token=claim_token,
                error="generated validation failed",
            )
            return False
        outcome = okf.publish_claimed_okf(
            cfg.state_dir,
            lease_owner=lease_owner,
            tool_name=tool_name,
            claim_token=claim_token,
            okf_path=path,
            publish=publish,
            rollback=rollback,
        )
        if outcome == "invalid":
            okf.mark_candidate_error(
                cfg.state_dir,
                tool_name=tool_name,
                claim_token=claim_token,
                error="generated validation failed",
            )
        elif outcome == "stale":
            logger.error("Discarding generated local-knowledge OKF for %s after ownership loss", tool_name)
        return outcome == "done"
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _fail_claimed_rows(cfg: RuntimeConfig, rows: list[dict[str, Any]], *, error: str) -> None:
    for row in rows:
        okf.mark_candidate_error(
            cfg.state_dir,
            tool_name=str(row.get("tool_name") or ""),
            claim_token=str(row.get("claim_token") or ""),
            error=error,
        )


def _generate_claimed_okfs(
    cfg: RuntimeConfig,
    *,
    llm: Any,
    rows: list[dict[str, Any]],
    lease_owner: str,
    can_publish: Callable[[], bool] | None = None,
) -> bool:
    packets = [_generation_packet(row, cfg.state_dir) for row in rows]
    result = llm.complete_structured(
        instructions=(
            "Create one compact routing note for every supplied Hermes tool candidate. "
            "Treat each candidate independently. Do not mention, contrast, or relate another candidate "
            "merely because it appears in the same batch. Use only the supplied privacy-safe structural "
            "packet. Never infer or request raw transcripts, tool outputs, document contents, emails, "
            "credentials, secret values, unsupported methods, permissions, side effects, enum choices, "
            "or capabilities. Return every tool and schema_hash exactly as supplied. Aliases and triggers "
            "must be concrete multi-word user intents that positively select this tool and include the "
            "relevant action and domain or object; avoid generic phrases. For when_not_to_use, include only "
            "a meaningful boundary from a genuine near-neighbor supported by this candidate's packet. Leave "
            "when_not_to_use empty when no such distinction is available; never use unrelated domains, "
            "missing-argument checks, credentials, secrets, privacy policy, or generic non-use cases. "
            "Every related_tools value must be an exact identifier from this candidate's "
            "allowed_related_tools and a genuine alternative or complementary step; leave it empty rather "
            "than guessing. Write an evergreen one-to-three-sentence body explaining only positive purpose "
            "and important required inputs. Keep selection boundaries, comparisons, and all negative/non-use "
            "guidance exclusively in when_not_to_use. Do not include counters, errors, timestamps, redaction "
            "notes, or unsupported behavior."
        ),
        input=[{"type": "text", "text": json.dumps({"candidates": packets}, ensure_ascii=False, sort_keys=True)}],
        json_schema=OKF_GENERATION_SCHEMA,
        schema_name="local_knowledge_tool_okfs",
        temperature=0.0,
        max_tokens=min(4_000, max(800, len(rows) * 1_000)),
        timeout=cfg.okf.max_generation_seconds,
        purpose="local_knowledge.okf_generation",
    )
    if can_publish is not None and not can_publish():
        logger.error("Discarding generated local-knowledge OKFs after generation lease loss")
        return False
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
        completed += int(_write_and_complete_item(cfg, row=row, item=item, lease_owner=lease_owner))
    return completed > 0


def _on_session_finalize(**kwargs: Any) -> bool:
    if _inside_okf_worker():
        return False
    try:
        cfg = _runtime_config()
        if not cfg.okf.enabled or not cfg.okf.auto_generate:
            return False
        stale_after = max(cfg.okf.max_generation_seconds * 2, 60)
        if not okf.has_generation_work(
            cfg.state_dir,
            min_use_count=cfg.okf.min_use_count,
            stale_after_seconds=stale_after,
            timeout_seconds=0.05,
        ):
            return False
        return _spawn_worker(cfg)
    except Exception:
        logger.exception("Failed to launch local-knowledge OKF worker during session finalization")
        return False


def _on_session_end(**kwargs: Any) -> bool:
    """Backward-compatible import alias; Hermes registration uses finalization."""

    return _on_session_finalize(**kwargs)
