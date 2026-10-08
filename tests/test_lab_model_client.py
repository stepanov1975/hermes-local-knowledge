"""Offline checks of the lab sender safety boundary; no actual auth/model calls."""
from __future__ import annotations

import os
from typing import Any, Iterator

import pytest

from scripts.lab_model_client import LAB_ROOT, ModelPaused, check_environment


def test_production_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", "/home/alex")
    with pytest.raises(ModelPaused, match="clean environment"):
        check_environment()


def test_exact_lab_environment_and_no_inherited_provider_override(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in {
        "HOME": LAB_ROOT + "/lab-auth-client", "HERMES_HOME": LAB_ROOT + "/lab-auth",
        "CODEX_HOME": LAB_ROOT + "/lab-auth-client/.codex", "TMPDIR": "/home/alex/.hermes/cache/scratch",
        "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/candidate/venv/bin:/usr/bin:/bin",
    }.items():
        monkeypatch.setenv(key, value)
    check_environment()
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unsafe-env")
    with pytest.raises(ModelPaused):
        check_environment()


@pytest.fixture
def sender_logging_guard() -> Iterator[None]:
    """The controller suppresses logging; do not leak that state into other tests."""
    import logging
    previous = logging.root.manager.disable
    try:
        yield
    finally:
        logging.disable(previous)


@pytest.mark.parametrize("failure", ["none", "retries", "wrong_model", "provider_error"])
@pytest.mark.parametrize("role,model,effort", [("generation", "gpt-6-luna", "xhigh"), ("judgment", "gpt-6.1-sol", "high")])
def test_official_adapter_wire_and_provenance(monkeypatch: pytest.MonkeyPatch, sender_logging_guard: None,
                                            role: str, model: str, effort: str, failure: str) -> None:
    import hashlib
    import json

    import importlib.metadata

    try:
        host_version = importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Official-adapter integration requires the pinned lab Hermes 0.19.0")
    if host_version != "0.19.0":
        pytest.skip("Official-adapter integration requires the pinned lab Hermes 0.19.0")
    httpx = pytest.importorskip("httpx")
    OpenAI = pytest.importorskip("openai").OpenAI
    auxiliary_client = pytest.importorskip("agent.auxiliary_client")
    auth = pytest.importorskip("hermes_cli.auth")
    from scripts import lab_model_client as lab

    requests = []

    def provider(request: Any) -> Any:
        requests.append(request)
        if failure == "provider_error":
            return httpx.Response(500, json={"error": {"message": "synthetic-secret-error", "type": "server_error"}})
        # Invented SSE response exercised through the real official adapter.
        events = [
            {"type": "response.output_item.done", "output_index": 0, "item": {
                "type": "message", "id": "fixture", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": '{"fixture":true}', "annotations": []}]}},
            {"type": "response.completed", "response": {"id": "fixture", "status": "completed", "output": [],
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join("data: " + json.dumps(event) + "\n\n" for event in events))

    http = httpx.Client(transport=httpx.MockTransport(provider))
    real = OpenAI(api_key="synthetic-fixture-only", base_url="https://chatgpt.com/backend-api/codex", max_retries=1 if failure == "retries" else 0, http_client=http)
    client = auxiliary_client.CodexAuxiliaryClient(real, model)
    resolved = []

    def resolve(**kwargs):
        resolved.append(kwargs)
        return client, "wrong-model" if failure == "wrong_model" else model

    monkeypatch.setattr(auxiliary_client, "resolve_provider_client", resolve)
    monkeypatch.setattr(auth, "resolve_codex_runtime_credentials", lambda **kwargs: {"fixture": True})
    # Bypass only private lab filesystem preconditions, not the provider adapter.
    monkeypatch.setattr(lab, "check_environment", lambda: None)
    assert lab.importlib.metadata.version("hermes-agent") == "0.19.0"
    if failure != "none":
        with pytest.raises(ModelPaused) as exc:
            lab._send("synthetic prompt", role)
        assert "synthetic-secret-error" not in str(exc.value)
        assert len(requests) == (1 if failure == "provider_error" else 0)
        assert real.is_closed()
        return
    response = lab._send("synthetic prompt", role)
    assert resolved == [{"provider": "openai-codex", "model": model}]
    assert len(requests) == 1 and real.is_closed()
    assert response["text"] == '{"fixture":true}'
    provenance = response["provenance"]
    assert provenance["role"] == role and provenance["requested_model"] == model
    assert provenance["requested_wire_reasoning"] == effort
    assert provenance["retries"] == 0 and provenance["fallback"] is False
    assert provenance["wire"] == {"role": role, "endpoint": lab.ENDPOINT, "wire_model": model,
        "wire_effort": effort, "store": False, "stream": True, "tools_count": 0,
        "prompt_sha256": hashlib.sha256(b"synthetic prompt").hexdigest()}
    assert "synthetic-fixture-only" not in json.dumps(response)


@pytest.mark.parametrize("defect", ["endpoint", "model", "effort", "store", "tools", "prompt", "stream"])
def test_wire_contract_fail_closed(defect: str) -> None:
    import json
    from types import SimpleNamespace
    from scripts.lab_model_client import ENDPOINT, inspect_request

    body = {"model": "gpt-6.1-sol", "reasoning": {"effort": "high"}, "store": False,
            "stream": True, "tools": [], "input": [{"role": "user", "content": "fixture"}]}
    endpoint = ENDPOINT
    if defect == "endpoint":
        endpoint = "https://example.invalid/responses"
    elif defect == "effort":
        body["reasoning"] = {"effort": "none"}
    elif defect == "prompt":
        body["input"] = [{"role": "user", "content": "different"}]
    else:
        body[defect] = {"model": "gpt-6-luna", "store": True, "tools": [{"type": "function"}], "stream": False}[defect]
    with pytest.raises(ModelPaused):
        inspect_request(SimpleNamespace(url=endpoint, content=json.dumps(body)), "fixture", "judgment")


def test_named_wrappers_use_fixed_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import lab_model_client as lab
    calls = []

    def sender(prompt: str, role: str) -> dict:
        calls.append((prompt, role))
        return {}

    monkeypatch.setattr(lab, "_locked_send", sender)
    lab.send_generation("generate")
    lab.send_judgment("validate")
    assert calls == [("generate", "generation"), ("validate", "judgment")]
