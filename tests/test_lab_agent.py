"""Synthetic model/Docker controls only: these tests make no API/container calls."""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from scripts import lab_agent as agent
from scripts import lab_agent_model as model

REVISION = "a" * 40
FINAL = {"hypothesis": "synthetic", "strategy": "none", "related_attempts": [],
         "revisit_reason": "", "changes": [], "checks": [], "lessons": [], "no_change": True}


@pytest.mark.parametrize("path", ["scripts/lab_agent.py", "tests/test_lab_agent.py",
    "tests/test_evaluation.py", "docs/autonomous-lab.md", ".env", "../secret", "/secret"])
def test_controller_paths_excluded(path: str) -> None:
    assert not agent.permitted(path)


@pytest.mark.parametrize("path", ["hermes_local_knowledge/index.py", "tests/test_index.py",
    "README.md", "docs/routing.md", "examples/config.yaml", "skills/router/SKILL.md"])
def test_public_paths_allowed(path: str) -> None:
    assert agent.permitted(path)


def test_export_exact_commit_and_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in ("hermes_local_knowledge/index.py", "scripts/lab_model_client.py",
                     "tests/test_lab_agent.py", "docs/autonomous-lab.md", "README.md"):
            content = b"synthetic source"
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    monkeypatch.setattr(agent, "run", lambda args: REVISION)
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, buf.getvalue()))
    workspace = agent.export(tmp_path / "attempt", tmp_path, REVISION)
    assert set(agent.identity(workspace)["files"]) == {"README.md", "hermes_local_knowledge/index.py"}
    with pytest.raises(ValueError):
        agent.export(tmp_path / "other", tmp_path, "HEAD")


def spec_for(name: str, binds: list[tuple[Path, str, bool]]) -> dict[str, Any]:
    return {"Id": "synthetic-id", "Name": "/" + name, "Image": agent.IMAGE,
            "Config": {"User": "10000:10000", "Labels": {"lk.lab": "ticket-69"}},
            "HostConfig": {"NetworkMode": "none", "Privileged": False, "ReadonlyRootfs": True,
                "Memory": 2147483648, "MemorySwap": 2147483648, "NanoCpus": 2000000000,
                "PidsLimit": 256, "CapDrop": ["ALL"], "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "SecurityOpt": ["no-new-privileges:true"]},
            "Mounts": [{"Type": "bind", "Source": str(p.resolve()), "Destination": t, "RW": rw}
                       for p, t, rw in binds]}


def test_mounts_are_narrow_and_review_readonly(tmp_path: Path) -> None:
    binds = agent.mounts(tmp_path / "attempt", tmp_path / "corpus", "review", True)
    assert all(not rw for _, t, rw in binds if t.startswith("/inputs/") or t == "/work/plugin")
    assert not any(t == "/inputs" or "private" in str(p) for p, t, _ in binds)
    spec = spec_for("lk-lab-synthetic", binds)
    agent.verify_worker_spec(spec, "lk-lab-synthetic", binds)
    for key, value in (("NetworkMode", "host"), ("Privileged", True), ("CapAdd", ["SYS_ADMIN"]),
                       ("ReadonlyRootfs", False), ("Devices", [{}]), ("PidMode", "host")):
        modified = {**spec, "HostConfig": {**spec["HostConfig"], key: value}}
        with pytest.raises(ValueError):
            agent.verify_worker_spec(modified, "lk-lab-synthetic", binds)
    spec["Mounts"].append({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/socket", "RW": True})
    with pytest.raises(ValueError):
        agent.verify_worker_spec(spec, "lk-lab-synthetic", binds)


@pytest.mark.parametrize("infinite", [False, True])
def test_shell_output_streamed_and_storage_bounded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, infinite: bool) -> None:
    calls = []
    children = []
    removed = []
    real_popen = subprocess.Popen
    # Translate fake Docker argv to fixed harmless Python, never evaluate command.
    code = ("import os; chunk=b'x'*8192; "
            + ("\nwhile True: os.write(1, chunk); os.write(2, chunk)" if infinite else
               "\nfor _ in range(128): os.write(1, chunk)\nos.write(2,b'error'); raise SystemExit(7)"))
    def fake(args: list[str], **kw: Any) -> Any:
        calls.append((args, kw))
        child = real_popen([sys.executable, "-c", code], **kw)
        children.append(child)
        return child
    monkeypatch.setattr(agent.subprocess, "Popen", fake)
    from scripts import lab_process_output
    monkeypatch.setattr(lab_process_output, "COMMAND_OUTPUT_BYTES", 1024 * 1024 + 5)
    monkeypatch.setattr(agent, "cleanup", lambda *args: removed.append(args))
    command = "$(host-looking-command); echo synthetic"
    raw = tmp_path / "raw.json"
    tracemalloc.start()
    try:
        if infinite:
            with pytest.raises(agent.SandboxOutputLimit) as caught:
                agent.sandbox("lk-lab-synthetic", command, raw, "synthetic-id")
            assert caught.value.cleanup_complete
        else:
            result = agent.sandbox("lk-lab-synthetic", command, raw, "synthetic-id")
            assert "TRUNCATED" in result and len(result) < 24200
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024, "output must not be accumulated in memory"
    assert calls[0][0] == ["docker", "exec", "synthetic-id", "/bin/sh", "-c", command]
    assert "shell" not in calls[0][1] and "timeout" not in calls[0][1]
    assert set(calls[0][1]["env"]) == {"PATH"}
    assert children[0].poll() is not None
    receipt = json.loads(raw.read_text())
    assert receipt["output_limit_exceeded"] is infinite
    assert sum(receipt["stored_bytes"].values()) == 1024 * 1024 + 5
    assert len(receipt["stdout"]) <= lab_process_output.PREVIEW_BYTES
    assert len(receipt["stderr"]) <= lab_process_output.PREVIEW_BYTES
    for key in ("stdout", "stderr"):
        path = tmp_path / receipt["files"][key]
        assert path.stat().st_size == receipt["stored_bytes"][key]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert removed == ([("lk-lab-synthetic", "synthetic-id")] if infinite else [])
    assert receipt["exit_code"] == children[0].returncode
    if not infinite:
        assert receipt["exit_code"] == 7


def setup_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], list[Any]]:
    events: list[Any] = []
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    dev, history = tmp_path / "development.json", tmp_path / "history.json"
    dev.write_text('{"synthetic":true}')
    history.write_text('[]')
    def export(attempt: Path, repo: Path, revision: str) -> Path:
        attempt.mkdir()
        for child in ("work/plugin", "private", "inputs", "home", "scratch", "output"):
            (attempt / child).mkdir(parents=True)
        (attempt / "work/plugin/README.md").write_text("synthetic")
        return attempt / "work/plugin"
    def run(args: list[str], **kw: Any) -> str:
        events.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": agent.IMAGE, "Config": {"Labels": {"org.opencontainers.image.revision": agent.SOURCE}}}])
        return "synthetic-id"
    monkeypatch.setattr(agent, "run", run)
    monkeypatch.setattr(agent, "export", export)
    monkeypatch.setattr(agent, "ownership", lambda *a: events.append(("ownership", a[-1])))
    monkeypatch.setattr(agent, "cleanup", lambda *a: events.append(("cleanup", a)))
    attempt = tmp_path / "attempt"
    monkeypatch.setattr(agent, "inspect", lambda name: spec_for(name, agent.mounts(attempt, corpus, "implementation", False)))
    return {"attempt": attempt, "repo": tmp_path, "revision": REVISION, "corpus": corpus,
            "development": dev, "history": history, "image": agent.IMAGE,
            "name": "lk-lab-synthetic", "role": "implementation", "task": "synthetic task"}, events


def review_inputs(kwargs: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import lab_experiment as lab, lab_review_evidence as proof
    from tests.test_lab_experiment import Harness
    repo = tmp_path / "review-repo"
    repo.mkdir()
    lab.git(repo, "init", "-q")
    (repo / "README.md").write_text("synthetic")
    (repo / "tests").mkdir()
    (repo / "tests/test_config.py").write_text("# synthetic admitted test fixture")
    (repo / "scripts").mkdir()
    (repo / "scripts/controller.py").write_text("# excluded synthetic controller")
    lab.git(repo, "add", ".")
    lab.git(repo, "commit", "-qm", "synthetic")
    revision = lab.git(repo, "rev-parse", "HEAD").decode().strip()
    checks_root = tmp_path / "review-checks"
    identity = {"synthetic": "fixture"}
    harness = Harness.__new__(Harness)
    harness.identity = identity
    harness.checks_passed = True
    tools = tmp_path / "synthetic-tools"
    tools.mkdir()
    checks = harness.checks(
        attempt=checks_root, repo=repo, revision=revision, image=agent.IMAGE,
        check_tools=tools, check_tools_sha256=lab.sha({}))
    binding = {"revision": revision, "image": agent.IMAGE, "controller": lab.sha(identity),
               "tree": lab.git(repo, "rev-parse", revision + "^{tree}").decode().strip(),
               "checks_sha256": lab.sha(checks)}
    packet = proof.build(repo=repo, revision=revision, binding=binding, report={},
                         checks_root=checks_root, proof=tmp_path / "review-proof")
    path = tmp_path / "review-evidence.json"
    lab.durable_json(path, packet)
    kwargs.update(repo=repo, revision=revision, evidence=path)
    original = agent.export
    def export(attempt: Path, repo: Path, revision: str) -> Path:
        workspace = original(attempt, repo, revision)
        (workspace / "tests").mkdir()
        (workspace / "tests/test_config.py").write_text("# synthetic admitted test fixture")
        for path in workspace.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
        return workspace
    monkeypatch.setattr(agent, "export", export)


@pytest.mark.parametrize("replacement", [False, True])
def test_disk_stop_during_blocked_model_is_exact_and_preserves_usage(tmp_path, monkeypatch, replacement):
    import threading
    from scripts import lab_disk_guard as disk
    real_cleanup = agent.cleanup
    kwargs, events = setup_session(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "cleanup", real_cleanup)
    trigger, sampled = threading.Event(), threading.Event()
    low = [False]
    running = {"synthetic-id": True, "unrelated-id": True}
    alerts = []
    def space(path):
        return {"available_blocks": 9 if low[0] else 20, "usable_blocks": 100}
    def monitor(self):
        assert trigger.wait(5)
        self.sample()
        sampled.set()
    def inspect(name):
        spec = spec_for(kwargs["name"], agent.mounts(kwargs["attempt"], kwargs["corpus"], "implementation", False))
        spec["Id"] = "unrelated-id" if replacement and low[0] else "synthetic-id"
        spec["State"] = {"Running": running[spec["Id"]]}
        return spec
    original_run = agent.run
    def run(args, **kw):
        if args[:2] == ["docker", "ps"]:
            return ""
        if args[:2] == ["docker", "kill"]:
            assert args[-1] == "synthetic-id"
            assert alerts, "durable pause callback precedes worker stop"
            running["synthetic-id"] = False
        return original_run(args, **kw)
    def send(*args):
        low[0] = True
        trigger.set()
        assert sampled.wait(5), "monitor must act before provider returns"
        assert alerts and running["unrelated-id"]
        assert running["synthetic-id"] is replacement
        return {"usage": {"input_tokens": 23}, "message": {"content": json.dumps(FINAL)}}
    monkeypatch.setattr(disk, "available_space", space)
    monkeypatch.setattr(disk.DiskGuard, "_monitor", monitor)
    monkeypatch.setattr(agent, "inspect", inspect)
    monkeypatch.setattr(agent, "run", run)
    monkeypatch.setattr(model, "send", send)
    with disk.DiskGuard(tmp_path, alerts.append):
        with pytest.raises(ValueError if replacement else disk.LabDiskPaused):
            agent.run_session(**kwargs)
    status = json.loads((kwargs["attempt"] / "private/status.json").read_text())
    assert status["complete"] is False and status["usage"] == [{"input_tokens": 23}]
    assert status["cleanup_complete"] is (not replacement)
    assert (["docker", "rm", "-f", "synthetic-id"] in events) is (not replacement)
    assert ["docker", "rm", "-f", "unrelated-id"] not in events
    assert not any(t.name == "lab-disk-guard" for t in threading.enumerate())


def test_real_session_loop_fake_model_and_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs, events = setup_session(tmp_path, monkeypatch)
    requests = []
    responses = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function",
            "function": {"name": "sandbox", "arguments": '{"command":"pwd"}'}}]},
        {"role": "assistant", "content": json.dumps(FINAL), "tool_calls": []}]
    def send(messages: Any, receipt: Path) -> dict[str, Any]:
        requests.append(json.loads(json.dumps(messages)))
        assert (receipt.parent / "session.json").is_file()
        return {"message": responses.pop(0), "usage": {"input_tokens": 12, "output_tokens": 2}}
    monkeypatch.setattr(model, "send", send)
    monkeypatch.setattr(agent, "sandbox", lambda *a: "synthetic /work/plugin")
    result = agent.run_session(**kwargs)
    assert result["complete"] and result["final"]["no_change"]
    assert result["cleanup_complete"] is True
    assert result["revision"] == REVISION
    assert requests[1][-1] == {"role": "tool", "tool_call_id": "call1", "content": "synthetic /work/plugin"}
    assert len(result["usage"]) == 2
    assert events[-2][0] == "cleanup" and events[-1][0] == "ownership"
    assert json.loads((kwargs["attempt"] / "private/result.json").read_text()) == result


@pytest.mark.parametrize("role", ["implementation", "review"])
def test_session_prompt_defines_authoritative_source_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            role: str) -> None:
    kwargs, _ = setup_session(tmp_path, monkeypatch)
    kwargs["role"] = role
    if role == "review":
        review_inputs(kwargs, tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "inspect", lambda name: spec_for(
        name, agent.mounts(kwargs["attempt"], kwargs["corpus"], role, role == "review")))
    requests = []
    final = FINAL if role == "implementation" else {
        "approved": True, "shortcuts_checked": True, "compatibility_checked": True,
        "report_checked": True, "findings": []}

    def send(messages: Any, receipt: Path) -> dict[str, Any]:
        requests.append(json.loads(json.dumps(messages)))
        return {"message": {"role": "assistant", "content": json.dumps(final)}, "usage": {}}

    monkeypatch.setattr(model, "send", send)
    agent.run_session(**kwargs)
    prompt = requests[0][0]["content"]
    if role == "implementation":
        assert "Source /work/plugin is writable for implementation" in prompt
        assert "Apply intended candidate edits directly to /work/plugin" in prompt
        assert "no_change=true only when /work/plugin has no candidate edits" in prompt
    else:
        assert "Source /work/plugin is read-only for review; do not edit it" in prompt
        assert "Apply intended candidate edits" not in prompt
    assert "Only /work/plugin is imported as the candidate" in prompt
    assert "/output files are not automatically applied" in prompt
    assert "source path and digest" in prompt
    assert "must not claim verification of a different tree" in prompt


def test_pause_no_retry_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs, events = setup_session(tmp_path, monkeypatch)
    calls = []
    def send(*a: Any) -> Any:
        calls.append(a)
        raise model.ModelPaused("safe synthetic quota pause")
    monkeypatch.setattr(model, "send", send)
    with pytest.raises(model.ModelPaused):
        agent.run_session(**kwargs)
    assert len(calls) == 1
    assert events[-2][0] == "cleanup" and events[-1][0] == "ownership"
    status = json.loads((kwargs["attempt"] / "private/status.json").read_text())
    assert status["complete"] is False and status["usage"] == [{"available": False}]


def test_unknown_tool_refused_before_any_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs, _ = setup_session(tmp_path, monkeypatch)
    monkeypatch.setattr(model, "send", lambda *a: {"usage": {"available": False}, "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function",
        "function": {"name": "host_shell", "arguments": '{"command":"bad"}'}}]}})
    monkeypatch.setattr(agent, "sandbox", lambda *a: pytest.fail("must not execute"))
    with pytest.raises(model.ModelPaused, match="invalid/unknown"):
        agent.run_session(**kwargs)


def test_final_schema_and_source_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent.validate_final(FINAL, "implementation")
    with pytest.raises(model.ModelPaused):
        agent.validate_final({**FINAL, "no_change": "true"}, "implementation")
    with pytest.raises(model.ModelPaused):
        agent.validate_final({"approved": True}, "review")
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("owned fixture outside the source boundary")
    assert outside.is_file() and not outside.is_symlink()
    (source / "README.md").symlink_to(outside)
    assert (source / "README.md").resolve(strict=True) == outside
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("symlink target must not be read"))
    with pytest.raises(ValueError, match="symlink"):
        agent.identity(source)


def test_cleanup_exact_identity_and_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(agent, "inspect", lambda name: spec_for(name, []))
    def fake_run(args: list[str]) -> str:
        calls.append(args)
        return ""
    monkeypatch.setattr(agent, "run", fake_run)
    with pytest.raises(ValueError):
        agent.cleanup("lk-lab-synthetic", "wrong-id")
    assert not calls
    agent.cleanup("lk-lab-synthetic", "synthetic-id")
    assert calls[0] == ["docker", "rm", "-f", "synthetic-id"]
    assert calls[1][1:3] == ["ps", "-aq"]


@pytest.mark.parametrize("pressure", [False, True])
def test_session_worker_start_is_disk_action_fenced(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pressure: bool) -> None:
    from contextlib import contextmanager
    from scripts import lab_disk_guard as disk
    kwargs, events = setup_session(tmp_path, monkeypatch)
    alerts: list[dict[str, Any]] = []
    low = [False]
    inside = [False]
    monkeypatch.setattr(disk, "available_space", lambda path: {
        "available_blocks": 9 if low[0] else 20, "usable_blocks": 100})
    original_action = disk.action
    original_run = agent.run
    def inspect(name: str) -> dict[str, Any]:
        spec = spec_for(kwargs["name"], agent.mounts(kwargs["attempt"], kwargs["corpus"], "implementation", False))
        return {**spec, "State": {"Running": False}}
    monkeypatch.setattr(agent, "inspect", inspect)
    @contextmanager
    def dispatch_action() -> Any:
        low[0] = pressure
        guard.sample()  # Deterministic detection at the short start-dispatch boundary.
        with original_action():
            inside[0] = True
            try:
                yield
            finally:
                inside[0] = False
    def run(args: list[str], **options: Any) -> str:
        if args[:2] == ["docker", "start"]:
            assert inside[0], "worker start escaped the disk-action fence"
        return original_run(args, **options)
    monkeypatch.setattr(disk, "action", dispatch_action)
    monkeypatch.setattr(agent, "run", run)
    monkeypatch.setattr(model, "send", lambda *args: {
        "message": {"role": "assistant", "content": json.dumps(FINAL), "tool_calls": []},
        "usage": {"available": False}})
    with disk.DiskGuard(tmp_path, alerts.append) as guard:
        if pressure:
            with pytest.raises(disk.LabDiskPaused):
                agent.run_session(**kwargs)
        else:
            assert agent.run_session(**kwargs)["complete"]
    starts = [event for event in events if isinstance(event, list) and event[:2] == ["docker", "start"]]
    assert len(starts) == (0 if pressure else 1)
    assert bool(alerts) is pressure


def test_wire_inspection_uses_official_multiturn_conversion() -> None:
    httpx = pytest.importorskip("httpx")
    converter = pytest.importorskip("agent.codex_responses_adapter")._chat_messages_to_responses_input
    messages: list[dict[str, Any]] = [{"role": "system", "content": "synthetic system"}, {"role": "user", "content": "synthetic"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function",
                    "function": {"name": "sandbox", "arguments": '{"command":"pwd"}'}}]},
                {"role": "tool", "tool_call_id": "call1", "content": "synthetic result"}]
    body = {"model": model.MODEL, "reasoning": {"effort": "high", "summary": "auto"}, "store": False, "stream": True,
            "instructions": messages[0]["content"], "input": converter(messages[1:]),
            "tools": [{"type": "function", **model.TOOL["function"]}]}
    receipt = model.inspect_request(httpx.Request("POST", model.ENDPOINT, json=body), messages)
    assert receipt["tools"] == ["sandbox"]
    assert any(x.get("type") == "function_call_output" for x in body["input"])
    for key, value in (("model", "wrong"), ("store", True), ("tools", []), ("max_output_tokens", 1)):
        with pytest.raises(model.ModelPaused):
            model.inspect_request(httpx.Request("POST", model.ENDPOINT, json={**body, key: value}), messages)


@pytest.mark.parametrize("failure", ["ok", "quota", "ambiguous", "disk_pause"])
def test_official_adapter_fake_http_preserves_usage_and_pauses_safely(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    httpx = pytest.importorskip("httpx")
    openai = pytest.importorskip("openai")
    auxiliary = pytest.importorskip("agent.auxiliary_client")
    auth = pytest.importorskip("hermes_cli.auth")
    import logging
    previous_logging_disable = logging.root.manager.disable
    calls = []
    usage = {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16,
             "input_tokens_details": {"cached_tokens": 7}, "output_tokens_details": {"reasoning_tokens": 3}}
    events = [
        {"type": "response.output_item.done", "item": {"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(FINAL)}]}},
        {"type": "response.completed", "response": {"id": "synthetic-response", "status": "completed", "usage": usage}}]
    payload = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
    class SyntheticStream(httpx.SyncByteStream):  # type: ignore[name-defined]
        def __iter__(self) -> Any:
            for offset in range(0, len(payload), 7):
                yield payload[offset:offset + 7]
    def handle(request: Any) -> Any:
        calls.append(request)
        assert (tmp_path / "wire.json").is_file(), "receipt must precede dispatch"
        if failure == "quota":
            return httpx.Response(429, json={"error": {"message": "SYNTHETIC_SECRET_MUST_NOT_ESCAPE"}})
        if failure == "ambiguous":
            raise httpx.ReadError("SYNTHETIC_SECRET_MUST_NOT_ESCAPE: accepted then lost response")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SyntheticStream())
    client = openai.OpenAI(api_key="mock", base_url=model.ENDPOINT.rsplit("/", 1)[0],
                           max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    wrapper = auxiliary.CodexAuxiliaryClient(client, model.MODEL)
    from scripts import lab_disk_guard as disk
    low = [False]
    monkeypatch.setattr(disk, "available_space", lambda path: {
        "available_blocks": 9 if low[0] else 20, "usable_blocks": 100})
    def resolve(**kwargs: Any) -> Any:
        if failure == "disk_pause":
            low[0] = True
            guard.sample()  # Pressure detected while preparing the provider client.
        return wrapper, model.MODEL
    monkeypatch.setattr(auxiliary, "resolve_provider_client", resolve)
    monkeypatch.setattr(auth, "resolve_codex_runtime_credentials", lambda **kw: {})
    monkeypatch.setattr(model.importlib.metadata, "version", lambda name: "0.19.0")
    messages = [{"role": "system", "content": "synthetic system"}, {"role": "user", "content": "synthetic task"}]
    with disk.DiskGuard(tmp_path, lambda alert: model.save(tmp_path / "disk-alert.json", alert)) as guard:
        if failure != "ok":
            with pytest.raises(model.ModelPaused) as caught:
                model._send(messages, tmp_path / "wire.json")
            assert "SYNTHETIC_SECRET" not in str(caught.value)
        else:
            response = model._send(messages, tmp_path / "wire.json")
            assert response["usage"] == usage
            assert json.loads(response["message"]["content"]) == FINAL
    assert len(calls) == (0 if failure == "disk_pause" else 1)
    if failure == "disk_pause":
        assert (tmp_path / "disk-alert.json").is_file()
        assert not (tmp_path / "wire.json").exists()
    assert logging.root.manager.disable == previous_logging_disable


@pytest.mark.parametrize("tamper", [False, True])
def test_independent_review_session_is_revision_bound_readonly(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: bool) -> None:
    kwargs, _ = setup_session(tmp_path, monkeypatch)
    kwargs["role"] = "review"
    review_inputs(kwargs, tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "inspect", lambda name: spec_for(name, agent.mounts(
        kwargs["attempt"], kwargs["corpus"], "review", True)))
    final = {"approved": True, "shortcuts_checked": True, "compatibility_checked": True,
             "report_checked": True, "findings": []}
    def send(*args: Any) -> Any:
        if tamper:
            (kwargs["attempt"] / "work/plugin/README.md").write_text("synthetic tampering")
        return {"message": {"role": "assistant", "content": json.dumps(final), "tool_calls": []},
                "usage": {"available": False}}
    monkeypatch.setattr(model, "send", send)
    if tamper:
        with pytest.raises(ValueError, match="projection bytes/modes mismatch"):
            agent.run_session(**kwargs)
        assert not (kwargs["attempt"] / "private/result.json").exists()
    else:
        result = agent.run_session(**kwargs)
        receipt = json.loads((kwargs["attempt"] / "private/session.json").read_text())
        assert result["session_id"] == receipt["session_id"]
        assert result["role"] == "review" and result["revision"] == receipt["revision"]
        assert result["source_identity"] == receipt["source_identity"]
        create = agent.worker_command(kwargs["name"], agent.IMAGE, agent.mounts(
            kwargs["attempt"], kwargs["corpus"], "review", True))
        assert "none" == create[create.index("--network") + 1]
        assert not any("/inputs," in arg or "docker.sock" in arg for arg in create)


@pytest.mark.parametrize("role", ["implementation", "review"])
def test_paid_source_rejection_retains_usage_and_cleanup(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    kwargs, events = setup_session(tmp_path, monkeypatch)
    kwargs["role"] = role
    if role == "review":
        review_inputs(kwargs, tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "inspect", lambda name: spec_for(name, agent.mounts(
        kwargs["attempt"], kwargs["corpus"], role, role == "review")))
    usage = {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168}
    def send(*args: Any) -> Any:
        source = kwargs["attempt"] / "work/plugin"
        if role == "implementation":
            (source / "unapproved-secret.txt").write_text("SYNTHETIC_PRIVATE_CONTENT")
            final = FINAL
        else:
            (source / "README.md").write_text("SYNTHETIC_PRIVATE_CONTENT")
            final = {"approved": True, "shortcuts_checked": True, "compatibility_checked": True,
                     "report_checked": True, "findings": []}
        return {"message": {"role": "assistant", "content": json.dumps(final), "tool_calls": []},
                "usage": usage}
    monkeypatch.setattr(model, "send", send)
    with pytest.raises(ValueError):
        agent.run_session(**kwargs)
    private = kwargs["attempt"] / "private"
    status = json.loads((private / "status.json").read_text())
    session = json.loads((private / "session.json").read_text())
    assert status["usage"] == [usage]
    for field in ("role", "session_id", "revision", "model", "provider", "effort"):
        assert status[field] == session[field]
    assert status["cleanup_complete"] and status["final_received"]
    assert status["source_validation_attempted"] and not status["source_valid"]
    assert not status["complete"] and not (private / "result.json").exists()
    assert "SYNTHETIC_PRIVATE_CONTENT" not in (private / "status.json").read_text()
    assert events[-2][0] == "cleanup" and events[-1][0] == "ownership"


def test_cleanup_failure_still_retains_usage_without_restoring_ownership(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs, events = setup_session(tmp_path, monkeypatch)
    monkeypatch.setattr(model, "send", lambda *args: {
        "message": {"role": "assistant", "content": json.dumps(FINAL), "tool_calls": []},
        "usage": {"total_tokens": 168}})
    def fail(*args: Any) -> None:
        raise ValueError("SYNTHETIC_PRIVATE_CLEANUP_ERROR")
    monkeypatch.setattr(agent, "cleanup", fail)
    with pytest.raises(ValueError):
        agent.run_session(**kwargs)
    status_path = kwargs["attempt"] / "private/status.json"
    status = json.loads(status_path.read_text())
    assert status["usage"] == [{"total_tokens": 168}]
    assert not status["cleanup_complete"] and not status["complete"]
    assert not status["source_validation_attempted"]
    assert "SYNTHETIC_PRIVATE_CLEANUP_ERROR" not in status_path.read_text()
    assert [event for event in events if event[0] == "ownership"] == [("ownership", "10000:10000")]


def test_output_guard_session_does_not_remove_owned_worker_twice(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs, events = setup_session(tmp_path, monkeypatch)
    monkeypatch.setattr(model, "send", lambda *args: {
        "message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call1", "type": "function", "function": {
                "name": "sandbox", "arguments": '{"command":"synthetic output"}'}}]},
        "usage": {"total_tokens": 168}})
    def sandbox(name: str, command: str, raw: Path, container_id: str) -> str:
        agent.cleanup(name, container_id)
        raise agent.SandboxOutputLimit(container_id, True)
    monkeypatch.setattr(agent, "sandbox", sandbox)
    with pytest.raises(agent.SandboxOutputLimit):
        agent.run_session(**kwargs)
    status = json.loads((kwargs["attempt"] / "private/status.json").read_text())
    assert status["cleanup_complete"] and not status["complete"]
    assert status["usage"] == [{"total_tokens": 168}]
    assert len([event for event in events if event[0] == "cleanup"]) == 1


def test_private_receipts_mode_and_file_then_directory_fsync(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced = []
    real_fsync = os.fsync
    def fsync(fd: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)
    monkeypatch.setattr(model.os, "fsync", fsync)
    previous = os.umask(0)
    try:
        path = tmp_path / "receipt.json"
        model.save(path, {"synthetic": True})
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert synced == [False, True]
    with pytest.raises(FileExistsError):
        model.save(path, {})
