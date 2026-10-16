"""Offline tool-using implementation/review sessions controlled by trusted host code."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import uuid
from pathlib import Path
from typing import Any

from scripts import lab_agent_model as model
from scripts.lab_runtime import run, scoped_name
from scripts.lab_process_output import bounded_process
from scripts import lab_disk_guard as disk

IMAGE = "sha256:0b9b3c213a9a3cb6845f31b44a0a2b4ce67fa716d411217f6037cf792b3775af"
SOURCE = "90194c64ff99933ec3b9ecfd9295b06e6c11e038"
LABEL = "lk.lab=ticket-69"


def permitted(path: str) -> bool:
    """Positive worker export/ingestion policy; controller and private cases never export."""
    p = Path(path)
    if p.is_absolute() or ".." in p.parts or any(x.startswith(".") for x in p.parts):
        return False
    if path in {"README.md", "CONTRIBUTING.md", "AGENTS.md", "memory.md", "pyproject.toml", "plugin.yaml", "LICENSE", "after-install.md"}:
        return True
    if not p.parts or p.parts[0] not in {"hermes_local_knowledge", "tests", "docs", "skills", "examples"}:
        return False
    if p.parts[0] == "hermes_local_knowledge":
        return True
    # Lab/evaluator tooling is not part of a worker's permitted edit or inspection scope.
    blocked = ("lab", "autonomous", "benchmark", "evaluat", "controller", "experiment", "model_client")
    return not any(token in path.lower() for token in blocked)


def identity(workspace: Path) -> dict[str, Any]:
    files = {}
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace).as_posix()
        if path.is_symlink():
            raise ValueError("Worker source contains a symlink")
        if path.is_file():
            if any(x in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"} for x in path.parts):
                continue
            if not permitted(relative):
                raise ValueError("Worker wrote outside permitted source scope")
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise ValueError("Worker source contains a nonregular file")
    encoded = json.dumps(files, sort_keys=True).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "files": files}


def export(attempt: Path, repo: Path, revision: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Exact commit required")
    if run(["git", "-C", str(repo), "rev-parse", revision + "^{commit}"]).strip() != revision:
        raise ValueError("Commit mismatch")
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", revision],
                             env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                             check=True, capture_output=True, timeout=120).stdout
    attempt.mkdir(mode=0o700)
    workspace = attempt / "work/plugin"
    workspace.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not permitted(member.name):
                continue
            if not (member.isfile() or member.isdir()):
                raise ValueError("Unsafe source archive")
            path = workspace / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                stream = tar.extractfile(member)
                assert stream is not None
                with stream:
                    path.write_bytes(stream.read())
                path.chmod(member.mode & 0o777)
    for child in ("home", "output", "scratch", "private", "inputs"):
        (attempt / child).mkdir(mode=0o700)
    return workspace


def mounts(attempt: Path, corpus: Path, role: str, evidence: bool) -> list[tuple[Path, str, bool]]:
    values = [(attempt / "home", "/opt/data", True),
              (attempt / "work/plugin", "/work/plugin", role == "implementation"),
              (attempt / "scratch", "/scratch", True), (attempt / "output", "/output", True),
              (corpus, "/corpus", False),
              (attempt / "inputs/development.json", "/inputs/development.json", False),
              (attempt / "inputs/history.json", "/inputs/history.json", False)]
    if evidence:
        values.append((attempt / "inputs/evidence.json", "/inputs/evidence.json", False))
    for source, _, _ in values:
        if "," in str(source.resolve()):
            raise ValueError("Invalid bind path")
    return values


def worker_command(name: str, image: str, binds: list[tuple[Path, str, bool]]) -> list[str]:
    scoped_name(name)
    if image != IMAGE:
        raise ValueError("Only fixed official image permitted")
    args = ["docker", "create", "--name", name, "--label", LABEL, "--network", "none",
            "--user", "10000:10000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--read-only", "--memory", "2g", "--memory-swap", "2g", "--cpus", "2",
            "--pids-limit", "256", "--restart", "no", "--init", "--workdir", "/work/plugin",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
            "--env", "HOME=/opt/data", "--env", "HERMES_HOME=/opt/data",
            "--env", "CODEX_HOME=/opt/data/no-codex", "--env", "TMPDIR=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTEST_ADDOPTS=-p no:cacheprovider",
            "--env", "RUFF_CACHE_DIR=/scratch/ruff-cache", "--env", "MYPY_CACHE_DIR=/scratch/mypy-cache",
            "--entrypoint", "/bin/sleep"]
    for source, target, rw in binds:
        args += ["--mount", f"type=bind,src={source.resolve()},dst={target}" + ("" if rw else ",readonly")]
    return args + [image, "infinity"]


def verify_worker_spec(spec: dict[str, Any], name: str, binds: list[tuple[Path, str, bool]]) -> None:
    config, host = spec["Config"], spec["HostConfig"]
    expected = {"NetworkMode": "none", "Privileged": False, "ReadonlyRootfs": True,
                "Memory": 2147483648, "MemorySwap": 2147483648, "NanoCpus": 2000000000,
                "PidsLimit": 256, "CapDrop": ["ALL"], "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}}
    if (spec["Image"] != IMAGE or spec["Name"] != "/" + name
            or config["User"] != "10000:10000" or config["Labels"].get("lk.lab") != "ticket-69"
            or any(host.get(k) != v for k, v in expected.items()) or host.get("CapAdd")
            or host.get("Devices") or host.get("DeviceRequests") or host.get("PidMode")
            or host.get("IpcMode") == "host" or host.get("VolumesFrom")
            or "no-new-privileges:true" not in host["SecurityOpt"]):
        raise ValueError("Offline worker isolation mismatch")
    actual = {(m["Type"], m["Source"], m["Destination"], m["RW"]) for m in spec["Mounts"]}
    actual.discard(("tmpfs", "", "/tmp", True))
    if actual != {("bind", str(p.resolve()), target, rw) for p, target, rw in binds}:
        raise ValueError("Offline worker mounts mismatch")


def inspect(name: str) -> dict[str, Any]:
    return json.loads(run(["docker", "inspect", name]))[0]  # type: ignore[no-any-return]


def cleanup(name: str, container_id: str) -> None:
    spec = inspect(name)
    if (spec["Id"] != container_id or spec["Name"] != "/" + name
            or spec["Config"]["Labels"].get("lk.lab") != "ticket-69"):
        raise ValueError("Refusing unowned cleanup")
    run(["docker", "rm", "-f", container_id])
    if run(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"]).strip():
        raise ValueError("Worker removal not confirmed")


def ownership(attempt: Path, image: str, name: str, owner: str) -> None:
    """Trusted one-shot helper only mounts this fresh session's mutable exports."""
    args = ["docker", "run", "--rm", "--name", scoped_name(name + "-setup"), "--label", LABEL,
            "--network", "none", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
            "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges:true", "--read-only",
            "--memory", "64m", "--memory-swap", "64m", "--cpus", "0.25", "--pids-limit", "16",
            "--entrypoint", "/bin/chown"]
    children = ("home", "work", "scratch", "output")
    for child in children:
        args += ["--mount", f"type=bind,src={attempt / child},dst=/{child}"]
    run(args + [image, "-R", "--no-dereference", owner, *["/" + c for c in children]])
    if run(["docker", "ps", "-aq", "--filter", "name=^/" + name + "-setup$"]).strip():
        raise ValueError("Ownership helper removal not confirmed")


class SandboxOutputLimit(model.ModelPaused):
    def __init__(self, container_id: str, cleanup_complete: bool):
        super().__init__("Sandbox output storage ceiling exceeded; session paused")
        self.container_id = container_id
        self.cleanup_complete = cleanup_complete


def sandbox(name: str, command: str, raw: Path, container_id: str) -> str:
    """Stream private output with bounded RAM/storage; shell exists only in Docker."""
    result = bounded_process(["docker", "exec", container_id, "/bin/sh", "-c", command],
                             prefix=raw, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                             on_limit=lambda: cleanup(name, container_id))
    receipt = result.receipt
    removed = receipt["limit_action_complete"]
    model.save(raw, {"command": command, **receipt, "cleanup_complete": removed})
    if receipt["output_limit_exceeded"]:
        raise SandboxOutputLimit(container_id, removed)
    output = json.dumps({"exit_code": result.returncode,
                         "stdout": receipt["stdout"], "stderr": receipt["stderr"]})
    if any(receipt["stored_bytes"][key] > len(getattr(result, key)) for key in ("stdout", "stderr")) or len(output) > 24000:
        return output[:24000] + "\n[TRUNCATED: raw output retained privately by controller]"
    return output


def validate_final(value: Any, role: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise model.ModelPaused("Session final must be a JSON object")
    if role == "implementation":
        fields = {"hypothesis", "strategy", "related_attempts", "revisit_reason", "changes", "checks", "lessons", "no_change"}
        valid = (type(value.get("no_change")) is bool
                 and all(isinstance(value.get(k), str) for k in ("hypothesis", "strategy", "revisit_reason"))
                 and all(isinstance(value.get(k), list) for k in ("related_attempts", "changes", "checks", "lessons")))
    else:
        fields = {"approved", "shortcuts_checked", "compatibility_checked", "report_checked", "findings"}
        valid = (all(type(value.get(k)) is bool for k in fields - {"findings"})
                 and isinstance(value.get("findings"), list))
    if not fields <= value.keys() or not valid:
        raise model.ModelPaused("Session final schema mismatch")
    return value


def run_session(*, attempt: Path, repo: Path, revision: str, corpus: Path,
                development: Path, history: Path, image: str, name: str, role: str,
                task: str, evidence: Path | None = None) -> dict[str, Any]:
    if role not in {"implementation", "review"} or image != IMAGE:
        raise ValueError("Fixed role/image required")
    disk.checkpoint()
    scoped_name(name + "-setup")
    attempt = attempt.absolute()
    if not corpus.is_dir() or any(not p.is_file() for p in [development, history, *([evidence] if evidence else [])]):
        raise ValueError("Narrow input paths required")
    image_spec = json.loads(run(["docker", "image", "inspect", image]))[0]
    if image_spec["Id"] != IMAGE or image_spec["Config"]["Labels"].get("org.opencontainers.image.revision") != SOURCE:
        raise ValueError("Official image identity mismatch")
    disk.checkpoint()
    workspace = export(attempt, repo, revision)
    for source, filename in ((development, "development.json"), (history, "history.json"), (evidence, "evidence.json")):
        if source is not None:
            # Inputs are caller-projected safe development/history/aggregate evidence only.
            model.save(attempt / "inputs" / filename, json.loads(source.read_text()))
            (attempt / "inputs" / filename).chmod(0o444)
    (attempt / "inputs").chmod(0o755)
    session_id = str(uuid.uuid4())
    private = attempt / "private"
    initial = identity(workspace)
    model.save(private / "session.json", {"role": role, "session_id": session_id,
               "revision": revision, "source_identity": initial, "model": model.MODEL,
               "provider": model.PROVIDER, "effort": model.EFFORT})
    schema = ("hypothesis,strategy,related_attempts,revisit_reason,changes,checks,lessons,no_change (boolean)"
              if role == "implementation" else
              "approved,shortcuts_checked,compatibility_checked,report_checked (all booleans),findings (list)")
    if role == "implementation":
        schema += "; hypothesis,strategy,revisit_reason must be strings; related_attempts,changes,checks,lessons must be lists"
    messages: list[dict[str, Any]] = [{"role": "system", "content":
        "You are an isolated Local Knowledge plugin " + role + ". Product goal: fast relevant whole-artifact routing. "
        "Only sandbox(command) is available. All commands run offline as nonroot. Source: /work/plugin; "
        "corpus: /corpus; development: /inputs/development.json; past reports: /inputs/history.json; "
        "optional safe evidence: /inputs/evidence.json. Writable scratch: /scratch, home: /opt/data, output: /output. "
        "Never attempt credentials, network, orchestration/evaluation changes or held-out labels. "
        "Do not launch model calls or background workers. Source is read-only for review. "
        "Inspect public contracts, make reusable plugin improvements only, run meaningful checks. "
        "A useful no-change outcome is permitted. Explain revisiting prior failed strategies. "
        "Review checks shortcuts, compatibility and report/execution correspondence on this exact revision. "
        "Finish with ONLY a JSON object containing: " + schema}, {"role": "user", "content": task}]
    binds = mounts(attempt, corpus, role, evidence is not None)
    container_id: str | None = None
    changed_owner = False
    worker_removed = False
    usages = []
    complete = False
    lifecycle = ExitStack()
    try:
        disk.checkpoint()
        ownership(attempt, image, name, "10000:10000")
        changed_owner = True
        disk.checkpoint()
        container_id = run(worker_command(name, image, binds)).strip()
        spec = inspect(name)
        verify_worker_spec(spec, name, binds)
        model.save(private / "worker.json", spec)
        def stop_disk() -> None:
            current = inspect(name)
            if (current["Id"] != container_id or current["Name"] != "/" + name
                    or current["Config"]["Labels"].get("lk.lab") != "ticket-69"):
                raise ValueError("Disk stop ownership mismatch")
            if current["State"]["Running"]:
                run(["docker", "kill", container_id])
            stopped = inspect(container_id)
            if (stopped["Id"] != container_id or stopped["Name"] != "/" + name
                    or stopped["Config"]["Labels"].get("lk.lab") != "ticket-69"
                    or stopped["State"]["Running"]):
                raise ValueError("Disk stop not confirmed")

        lifecycle.enter_context(disk.owned_worker(stop_disk))
        with disk.action():
            run(["docker", "start", container_id])
        disk.checkpoint()
        turn = 0
        while True:
            disk.checkpoint()
            turn += 1
            model.save(private / f"request-{turn}.json", messages)
            usages.append({"available": False})
            disk.checkpoint()
            response = model.send(messages, private / f"wire-{turn}.json")
            usages[-1] = response["usage"]
            disk.checkpoint()
            model.save(private / f"response-{turn}.json", response)
            message = response["message"]
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                try:
                    final = validate_final(json.loads(message["content"]), role)
                except (ValueError, TypeError):
                    raise model.ModelPaused("Session did not return final JSON") from None
                complete = True
                break
            ids = set()
            # Validate the entire batch before any side effects.
            commands = []
            for call in calls:
                arguments: Any = None
                try:
                    arguments = json.loads(call["function"]["arguments"])
                    valid = (call["type"] == "function" and call["function"]["name"] == "sandbox"
                             and isinstance(call["id"], str) and bool(call["id"]) and call["id"] not in ids
                             and set(arguments) == {"command"} and isinstance(arguments["command"], str))
                except (ValueError, KeyError, TypeError):
                    valid = False
                if not valid:
                    raise model.ModelPaused("Session requested an invalid/unknown tool")
                ids.add(call["id"])
                commands.append(arguments["command"])
            for index, (call, command) in enumerate(zip(calls, commands, strict=True)):
                disk.checkpoint()
                output = sandbox(name, command, private / f"tool-{turn}-{index}.json", container_id)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    except SandboxOutputLimit as error:
        worker_removed = error.container_id == container_id and error.cleanup_complete
        raise
    finally:
        lifecycle.close()
        cleanup_complete = False
        source_valid = False
        source_validation_attempted = False
        source_identity = None
        try:
            # Never restore ownership while a model-selected process can still write.
            if container_id is not None and not worker_removed:
                cleanup(name, container_id)
            if changed_owner:
                ownership(attempt, image, name, f"{os.getuid()}:{os.getgid()}")
            cleanup_complete = True
            if complete:
                source_validation_attempted = True
                source_identity = identity(workspace)
                if role == "review" and source_identity != initial:
                    raise ValueError("Read-only review source changed")
                source_valid = True
        finally:
            # Billed usage and cleanup evidence survive source/cleanup rejection.
            model.save(private / "status.json", {
                "complete": complete and source_valid and cleanup_complete,
                "final_received": complete, "usage": usages,
                "role": role, "session_id": session_id, "revision": revision,
                "model": model.MODEL, "provider": model.PROVIDER, "effort": model.EFFORT,
                "cleanup_complete": cleanup_complete,
                "source_validation_attempted": source_validation_attempted,
                "source_valid": source_valid})
    result = {"role": role, "session_id": session_id, "revision": revision,
              "model": model.MODEL, "provider": model.PROVIDER, "effort": model.EFFORT,
              "workspace": str(workspace), "final": final, "usage": usages,
              "complete": True, "source_identity": source_identity}
    model.save(private / "result.json", result)
    return result
