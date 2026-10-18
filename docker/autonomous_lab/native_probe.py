"""Run via the sealed official interpreter, not by importing a standalone index.

Results are private worker observations, not a trusted campaign evaluator.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--blocked", action="append", default=[])
    args = parser.parse_args()
    home = Path(os.environ["HERMES_HOME"])
    output = Path("/output")
    before = {name: (home / name).exists() for name in
              ("knowledge", "memories", "feedback-canary", "auth.json", "cron", "state.db")}
    assert not any(before.values()), before
    assert os.getuid() == 10000
    assert not Path("/var/run/docker.sock").exists()
    assert not Path("/root/.ssh").exists()
    assert not Path("/opt/data/.env").exists()
    started = time.monotonic()
    # Official discovery loads only the exported plugin directory in clean HERMES_HOME.
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    manager.discover_and_load()
    from tools.registry import registry
    entries = [entry for entry in registry.get_all_entries() if entry.name == "knowledge_search"]
    assert len(entries) == 1, entries
    entry = entries[0]
    handler_file = sys.modules[entry.handler.__module__].__file__
    assert handler_file and handler_file.startswith(str(home / "plugins")), handler_file
    from model_tools import handle_function_call
    dispatch_file = sys.modules["model_tools"].__file__
    assert dispatch_file and dispatch_file.startswith("/opt/hermes/"), dispatch_file
    config_module = importlib.import_module(entry.handler.__module__.rsplit(".", 1)[0] + ".config")
    config = config_module.resolve_config()
    assert str(config.source_root) == "/corpus"
    assert not config.okf.enabled and not config.okf.auto_generate
    assert not config.implicit_feedback.enabled and config.verified_routing.mode == "off"
    # Exactly one public search request with limit=5; no synthesized/truncated ranking.
    call_args = {"query": args.query, "limit": 5}
    raw = handle_function_call("knowledge_search", call_args, task_id="lk-lab-pilot",
                               session_id="lk-lab-pilot", tool_call_id="lk-lab-search")
    (output / "native-search.json").write_text(raw, encoding="utf-8")
    result = json.loads(raw)
    assert not result.get("error"), result
    assert result.get("results"), result
    assert len(result["results"]) <= 5
    corpus_files = sorted(path for path in Path("/corpus").glob("*.md"))
    network: list[dict[str, object]] = []
    for host, path in (("chatgpt.com", "/backend-api/codex"), ("auth.openai.com", "/")):
        ips = sorted({row[4][0] for row in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)})
        observations = []
        for ip in ips:
            try:
                with socket.create_connection((ip, 443), timeout=8) as sock:
                    context = ssl.create_default_context()
                    context.minimum_version = ssl.TLSVersion.TLSv1_2
                    with context.wrap_socket(sock, server_hostname=host) as tls:
                        tls.settimeout(8)
                        tls.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                        line = tls.recv(1024).split(b"\r\n", 1)[0].decode("ascii", "replace")
                        assert line.startswith("HTTP/"), line
                        observations.append({"ip": ip, "tls": tls.version(), "http_status_line": line})
            except OSError as exc:
                observations.append({"ip": ip, "error": type(exc).__name__})
        assert any("http_status_line" in item for item in observations), observations
        network.append({"provider": host, "observations": observations,
                        "scope": "TLS/HTTP reachability only; no credentials or inference"})
    for target in args.blocked:
        host, port = target.rsplit(":", 1)
        host = host.strip("[]")
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                raise AssertionError("Forbidden network destination reachable: " + target)
        except OSError as exc:
            network.append({"blocked": target, "error": type(exc).__name__})
    # Real mutations in first run are deliberately absent at every subsequent start.
    (home / "memories").mkdir(exist_ok=True)
    (home / "memories/lab-canary.md").write_text("reset must remove previous memory\n")
    (home / "feedback-canary").write_text("reset must remove previous feedback\n")
    # Feedback uses the same native boundary, not a fake file as the only proof.
    feedback = handle_function_call("knowledge_feedback", {
        "artifact_id": result["results"][0]["id"], "query": args.query, "rating": "useful",
        "note": "Dedicated pilot reset evidence"}, task_id="lk-lab-pilot")
    (output / "native-feedback.json").write_text(feedback)
    assert not json.loads(feedback).get("error"), feedback
    (output / "checkpoint.json").write_text(json.dumps({"phase": "native-search-complete",
                                                        "next": "operator-controlled only"}) + "\n")
    report = {"uid": os.getuid(), "gid": os.getgid(), "initial_state": before,
              "isolation_authority": "Trusted controller Docker inspection (outside worker mounts)",
              "dispatcher": dispatch_file,
              "plugin_handler": handler_file, "plugin_toolset": entry.toolset,
              "effective_config": {"okf": False, "auto_generate": False,
                                   "implicit_feedback": False, "verified_routing": "off"},
              "search_args": call_args, "search_result_count": len(result["results"]),
              "corpus": [{"path": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in corpus_files], "network": network,
              "elapsed_seconds": time.monotonic() - started}
    (output / "pilot.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"native_search": "passed", "result_count": len(result["results"]),
                      "network": "passed", "checkpoint": "/output/checkpoint.json"}))


if __name__ == "__main__":
    main()
