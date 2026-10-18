"""Exercise the shipped network block without Hermes startup or real networking."""
from __future__ import annotations

import ast
from pathlib import Path
import socket
import ssl
from unittest.mock import MagicMock

import pytest


PROBE = Path(__file__).resolve().parents[1] / "docker/autonomous_lab/native_probe.py"


def _run_provider_block() -> list[dict[str, object]]:
    # Execute the actual statements from main(), not a copied implementation.
    # Exclude plugin discovery, file mutations and the separate blocked-host loop.
    tree = ast.parse(PROBE.read_text(encoding="utf-8"), filename=str(PROBE))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    start = next(i for i, node in enumerate(main.body)
                 if isinstance(node, ast.AnnAssign)
                 and isinstance(node.target, ast.Name) and node.target.id == "network")
    end = next(i for i, node in enumerate(main.body)
               if isinstance(node, ast.For) and isinstance(node.target, ast.Name)
               and node.target.id == "target")
    namespace = {"socket": socket, "ssl": ssl}
    exec(compile(ast.Module(body=main.body[start:end], type_ignores=[]), str(PROBE), "exec"), namespace)
    network = namespace["network"]
    assert isinstance(network, list)
    return network


@pytest.mark.parametrize("protocol", ["TLSv1.2", "TLSv1.3"])
def test_provider_probe_enforces_modern_verified_tls(
    monkeypatch: pytest.MonkeyPatch, protocol: str,
) -> None:
    real_factory = ssl.create_default_context
    contexts: list[ssl.SSLContext] = []
    wrapped_hosts: list[str] = []

    def context_factory() -> ssl.SSLContext:
        context = real_factory()
        # Simulate a runtime with a permissive default: the probe must set its
        # own floor rather than depending on this machine's OpenSSL defaults.
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        contexts.append(context)
        return context

    tls = MagicMock()
    tls.__enter__.return_value = tls
    tls.recv.return_value = b"HTTP/1.1 403 Forbidden\r\n\r\n"
    tls.version.return_value = protocol

    def wrap(context: ssl.SSLContext, sock: object, *, server_hostname: str) -> MagicMock:
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        wrapped_hosts.append(server_hostname)
        return tls

    connection = MagicMock()
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(socket, "getaddrinfo", MagicMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
    ]))
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(ssl, "create_default_context", context_factory)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap)

    network = _run_provider_block()

    assert len(contexts) == 2
    assert wrapped_hosts == ["chatgpt.com", "auth.openai.com"]
    assert connect.call_count == 2
    assert all(call.args == (("192.0.2.1", 443),) and call.kwargs == {"timeout": 8}
               for call in connect.call_args_list)
    assert network == [
        {"provider": host,
         "observations": [{"ip": "192.0.2.1", "tls": protocol,
                           "http_status_line": "HTTP/1.1 403 Forbidden"}],
         "scope": "TLS/HTTP reachability only; no credentials or inference"}
        for host in wrapped_hosts
    ]
    assert [call.args[0] for call in tls.sendall.call_args_list] == [
        b"GET /backend-api/codex HTTP/1.1\r\nHost: chatgpt.com\r\nConnection: close\r\n\r\n",
        b"GET / HTTP/1.1\r\nHost: auth.openai.com\r\nConnection: close\r\n\r\n",
    ]


def test_provider_probe_does_not_accept_certificate_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", MagicMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
    ]))
    monkeypatch.setattr(socket, "create_connection", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", MagicMock(
        side_effect=ssl.SSLCertVerificationError("untrusted certificate"),
    ))

    with pytest.raises(AssertionError, match="SSLCertVerificationError"):
        _run_provider_block()
