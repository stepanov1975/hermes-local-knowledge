"""Lab-only fixed Luna generation/Sol judgment; never load production state."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
from typing import Any

ROLES = {"generation": ("gpt-6-luna", "xhigh"), "judgment": ("gpt-6.1-sol", "high")}
ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
PROVIDER = "openai-codex"
LAB_ROOT = "/home/alex/.local/share/lk-autonomous-lab-execution"


class ModelPaused(RuntimeError):
    """Safe exception: provider bodies/credentials must not escape the adapter."""


def check_environment() -> None:
    expected = {
        "HOME": LAB_ROOT + "/lab-auth-client",
        "HERMES_HOME": LAB_ROOT + "/lab-auth",
        "CODEX_HOME": LAB_ROOT + "/lab-auth-client/.codex",
        "TMPDIR": "/home/alex/.hermes/cache/scratch",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if (any(os.environ.get(k) != v for k, v in expected.items())
            or set(os.environ) - {*expected, "PATH", "LC_CTYPE"}):
        raise ModelPaused("Lab sender requires the documented clean environment")


def send_generation(prompt: str) -> dict[str, Any]:
    return _locked_send(prompt, "generation")


def send_judgment(prompt: str) -> dict[str, Any]:
    return _locked_send(prompt, "judgment")


def _locked_send(prompt: str, role: str) -> dict[str, Any]:
    """Serialize shared lab grant ownership across all controller campaigns."""
    check_environment()
    import fcntl
    from pathlib import Path
    lock = Path(LAB_ROOT) / "lab-auth" / "benchmark-controller.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _send(prompt, role)
    finally:
        os.close(fd)


def inspect_request(request: Any, prompt: str, role: str) -> dict[str, Any]:
    """Allowlisted emitted inference metadata only; never headers or source text."""
    model, effort = ROLES[role]
    if str(request.url) != ENDPOINT:
        raise ModelPaused("Unexpected inference endpoint")
    body = json.loads(request.content)
    items = body.get("input", [])
    valid = (len(items) == 1 and items[0].get("role") == "user"
             and items[0].get("content") in (prompt, [{"type": "input_text", "text": prompt}]))
    if (body.get("model") != model or body.get("reasoning", {}).get("effort") != effort
            or body.get("store") is not False or body.get("stream") is not True
            or body.get("tools", []) != [] or not valid):
        raise ModelPaused("Inference wire contract mismatch")
    return {"role": role, "endpoint": ENDPOINT, "wire_model": model, "wire_effort": effort,
            "store": False, "stream": True, "tools_count": 0,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}


def _send(prompt: str, role: str) -> dict[str, Any]:
    """Called only while the trusted controller holds its exclusive campaign lock.

    Inference uses the official auxiliary API with inspected emitted settings,
    not a server-confirmed identity/effort echo.
    """
    check_environment()
    logging.disable(logging.CRITICAL)
    client: Any = None
    requested_model, effort = ROLES[role]
    wire: list[dict[str, Any]] = []
    try:
        if importlib.metadata.version("hermes-agent") != "0.19.0":
            raise ModelPaused("Expected official hermes-agent 0.19.0")
        from hermes_cli.auth import resolve_codex_runtime_credentials  # type: ignore[import-untyped]
        # Official controller-owned refresh, never persist/print the returned mapping.
        credentials = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        del credentials
        from agent.auxiliary_client import CodexAuxiliaryClient, resolve_provider_client  # type: ignore[import-untyped]
        client, model = resolve_provider_client(provider=PROVIDER, model=requested_model)
        if (not isinstance(client, CodexAuxiliaryClient) or model != requested_model
                or client._real_client.max_retries != 0):
            raise ModelPaused("Explicit no-retry role client unavailable; no fallback permitted")

        def inspect(request: Any) -> None:
            metadata = inspect_request(request, prompt, role)
            if wire:
                raise ModelPaused("Repeated inference refused")
            wire.append(metadata)

        client._real_client._client.event_hooks["request"].append(inspect)
        response = client.chat.completions.create(
            model=requested_model, messages=[{"role": "user", "content": prompt}], tools=[],
            extra_body={"reasoning": {"effort": effort}},
        )
        message = response.choices[0].message
        if message.tool_calls or not isinstance(message.content, str) or len(wire) != 1:
            raise ModelPaused("Unexpected non-text model response")
        return {
            "text": message.content,
            "provenance": {
                "role": role, "provider": PROVIDER, "requested_model": requested_model,
                "requested_wire_reasoning": effort, "hermes_version": "0.19.0",
                "wire": wire[0], "retries": 0, "fallback": False,
                "api": "agent.auxiliary_client.resolve_provider_client",
                "adapter_contract": "Codex Responses stream=true store=false tools=[] retries=0",
                "identity_evidence": "Inspected emitted wire settings; server identity/effort echoes not preserved",
                "scope": "trusted controller host; not worker network isolation",
            },
            "usage": {k: getattr(response.usage, k, None) for k in
                      ("prompt_tokens", "completion_tokens", "total_tokens")},
        }
    except ModelPaused:
        raise
    except Exception as exc:
        # Never stringify provider exceptions, auth responses, headers or bodies.
        status = getattr(exc, "status_code", None)
        raise ModelPaused(f"Lab model paused: {type(exc).__name__}, HTTP {status if isinstance(status, int) else 'unknown'}") from None
    finally:
        if client is not None:
            client.close()
