"""Trusted, no-retry Sol/high multi-message boundary; never mounted in workers."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
from typing import Any

from scripts.lab_model_client import ENDPOINT, LAB_ROOT, ModelPaused, check_environment

MODEL = "gpt-6.1-sol"
PROVIDER = "openai-codex"
EFFORT = "high"
TOOL: dict[str, Any] = {"type": "function", "function": {
    "name": "sandbox", "description": "Run a shell command in the isolated offline workspace.",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                   "required": ["command"], "additionalProperties": False}}}


def save(path: Path, value: Any) -> None:
    """Private receipts are durable before dispatch, not sandbox output files."""
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


def inspect_request(request: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
    from agent.codex_responses_adapter import _chat_messages_to_responses_input  # type: ignore[import-untyped]
    body = json.loads(request.content)
    expected_input = _chat_messages_to_responses_input([m for m in messages if m["role"] != "system"])
    fn = TOOL["function"]
    expected_tools = [{"type": "function", **fn}]
    allowed = {"model", "instructions", "input", "store", "stream", "reasoning", "include", "tools", "prompt_cache_key"}
    if (str(request.url) != ENDPOINT or request.method != "POST" or set(body) - allowed
            or body.get("model") != MODEL or body.get("reasoning") != {"effort": EFFORT, "summary": "auto"}
            or body.get("store") is not False or body.get("stream") is not True
            or body.get("tools") != expected_tools or body.get("input") != expected_input
            or body.get("instructions") != messages[0]["content"]):
        raise ModelPaused("Session inference wire contract mismatch")
    return {"provider": PROVIDER, "model": MODEL, "effort": EFFORT,
            "endpoint": ENDPOINT, "store": False, "stream": True, "retries": 0,
            "fallback": False, "tools": ["sandbox"],
            "request_sha256": hashlib.sha256(request.content).hexdigest()}


def send(messages: list[dict[str, Any]], receipt: Path) -> dict[str, Any]:
    """One inference, one shared grant lock. Ambiguous failures pause, never replay."""
    check_environment()
    fd = os.open(Path(LAB_ROOT) / "lab-auth/benchmark-controller.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _send(messages, receipt)
    except ModelPaused:
        raise
    except Exception:
        raise ModelPaused("Session inference paused; no retry or fallback") from None
    finally:
        os.close(fd)


def _send(messages: list[dict[str, Any]], receipt: Path) -> dict[str, Any]:
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    client: Any = None
    wire: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    try:
        if importlib.metadata.version("hermes-agent") != "0.19.0":
            raise ModelPaused("Expected official hermes-agent 0.19.0")
        from hermes_cli.auth import resolve_codex_runtime_credentials  # type: ignore[import-untyped]
        from agent.auxiliary_client import CodexAuxiliaryClient, resolve_provider_client  # type: ignore[import-untyped]
        import httpx

        credentials = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        del credentials
        client, model = resolve_provider_client(provider=PROVIDER, model=MODEL)
        if not isinstance(client, CodexAuxiliaryClient) or model != MODEL or client._real_client.max_retries != 0:
            raise ModelPaused("Official no-retry session client unavailable")

        def inspect(request: Any) -> None:
            if wire:
                raise ModelPaused("Repeated session inference refused")
            metadata = inspect_request(request, messages)
            save(receipt, metadata)
            wire.append(metadata)

        class UsageStream(httpx.SyncByteStream):
            """Observe SSE usage losslessly; official adapter otherwise drops details."""
            def __init__(self, source: Any):
                self.source = source

            def __iter__(self) -> Any:
                pending = b""
                for chunk in self.source:
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if line.startswith(b"data:"):
                            try:
                                event = json.loads(line[5:])
                                usage = event.get("response", {}).get("usage")
                                if event.get("type") == "response.completed" and isinstance(usage, dict):
                                    usages.append(usage)
                            except (ValueError, AttributeError):
                                pass
                    yield chunk

            def close(self) -> None:
                self.source.close()

        def observe(response: Any) -> None:
            if response.status_code == 200:
                response.stream = UsageStream(response.stream)

        hooks = client._real_client._client.event_hooks
        hooks["request"].append(inspect)
        hooks["response"].append(observe)
        try:
            response = client.chat.completions.create(
                model=MODEL, messages=messages, tools=[TOOL],
                extra_body={"reasoning": {"effort": EFFORT}})
        finally:
            hooks["request"].remove(inspect)
            hooks["response"].remove(observe)
        if len(wire) != 1:
            raise ModelPaused("Missing session wire receipt")
        message = response.choices[0].message
        calls = [{"id": c.id, "type": c.type, "function": {
            "name": c.function.name, "arguments": c.function.arguments}}
            for c in (message.tool_calls or [])]
        return {"message": {"role": "assistant", "content": message.content, "tool_calls": calls},
                "usage": usages[-1] if usages else {"available": False}, "wire": wire[0]}
    except ModelPaused:
        raise
    except Exception:
        raise ModelPaused("Session inference paused; provider details withheld; no retry") from None
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            logging.disable(previous_logging_disable)
