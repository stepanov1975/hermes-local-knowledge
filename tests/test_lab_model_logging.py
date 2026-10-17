"""Portable, offline sender logging proofs at controlled auth/provider boundaries."""
from __future__ import annotations

import json
import logging
import sys
from types import ModuleType, SimpleNamespace

import pytest

from scripts import lab_model_client as lab


@pytest.mark.parametrize("threshold", [logging.NOTSET, logging.ERROR, logging.CRITICAL + 7])
@pytest.mark.parametrize("failure", ["none", "version", "auth", "resolve", "contract", "provider", "close"])
def test_send_restores_logging_on_every_exit(monkeypatch, threshold, failure):
    previous = logging.root.manager.disable
    calls = []
    sensitive = "private provider detail"

    def boundary(name):
        assert logging.root.manager.disable == logging.CRITICAL
        calls.append(name)
        if failure == name:
            raise RuntimeError(sensitive)

    class Client:
        def __init__(self):
            self._real_client = SimpleNamespace(
                max_retries=1 if failure == "contract" else 0,
                _client=SimpleNamespace(event_hooks={"request": []}))
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            boundary("provider")
            request = SimpleNamespace(url=lab.ENDPOINT, content=json.dumps({
                "model": kwargs["model"], "reasoning": {"effort": "high"},
                "store": False, "stream": True, "tools": [],
                "input": [{"role": "user", "content": "fixture"}]}))
            for hook in self._real_client._client.event_hooks["request"]:
                hook(request)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="answer", tool_calls=[]))],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5))

        def close(self):
            boundary("close")

    client = Client()
    auth = ModuleType("hermes_cli.auth")
    auxiliary = ModuleType("agent.auxiliary_client")

    def authenticate(**kwargs):
        boundary("auth")
        return {"fixture": True}

    def resolve(**kwargs):
        boundary("resolve")
        return client, kwargs["model"]

    monkeypatch.setitem(auth.__dict__, "resolve_codex_runtime_credentials", authenticate)
    monkeypatch.setitem(auxiliary.__dict__, "CodexAuxiliaryClient", Client)
    monkeypatch.setitem(auxiliary.__dict__, "resolve_provider_client", resolve)
    # Install only controlled import boundaries; no real credentials, network or host imports.
    monkeypatch.setitem(sys.modules, "hermes_cli", ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    monkeypatch.setattr(lab, "check_environment", lambda: None)
    monkeypatch.setattr(lab.importlib.metadata, "version", lambda _: "unexpected" if failure == "version" else "0.19.0")
    try:
        logging.disable(threshold)
        if failure == "none":
            assert lab._send("fixture", "judgment")["text"] == "answer"
        else:
            with pytest.raises(lab.ModelPaused) as caught:
                lab._send("fixture", "judgment")
            assert sensitive not in str(caught.value)
        assert logging.root.manager.disable == threshold
        assert ("close" in calls) == (failure not in {"version", "auth", "resolve"})
    finally:
        logging.disable(previous)
