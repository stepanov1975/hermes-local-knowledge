"""Manually invoked, private, durable lab experiments; never production deployment.

The campaign manifest is the only incumbent/report authority. Pending attempts
are recovered under the campaign flock; ambiguous inference/ownership pauses,
never retries or times out valid work to make room.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid
from typing import Any, Callable, Iterator

from scripts.lab_evaluator import load_benchmark, require, verdict
from scripts.lab_runtime import validate_pin
from scripts.lab_process_output import bounded_process
from scripts import lab_disk_guard as disk
from scripts import lab_recovery as recovery

INITIAL_INCUMBENT = "a20a456f76db636740a1f2aef81b6aeb0f4f18a2"
SCHEMA = "private-manual-lab/v1"
MODEL = {"model": "gpt-6.1-sol", "provider": "openai-codex", "effort": "high"}
INPUT_KEYS = ("frozen", "frozen_sha256", "policy", "policy_sha256", "development",
              "development_sha256", "composite_id", "corpus", "manifest_sha256")
PATH_KEYS = {"frozen", "policy", "development", "corpus"}
IMPLEMENT_TASK = """Inspect the incumbent, development evidence and finalized prior reports.
Implement one justified hypothesis within hermes_local_knowledge/**/*.py or
examples configuration files only. Do not alter tests, controllers, benchmarks,
labels, generated state or dependencies. Return hypothesis, strategy,
related_attempts, revisit_reason, changes, checks, lessons and no_change.
If revisiting the same failed strategy, explain new evidence or a changed premise;
otherwise stop with no_change. Checks you report are advisory, never trusted gates.
Unknown returned top-five targets are unjudged, not automatically incorrect.
"""
REVIEW_TASK = """Independently inspect the declared filtered read-only candidate projection after actual trusted
execution. Verify no shortcuts/label tampering, official compatibility and that
the draft report corresponds to execution evidence. Return approved,
shortcuts_checked, compatibility_checked, report_checked booleans and findings.
Read /inputs/evidence.json and /inputs/review-proof. Verify projection paths/digests/Git modes,
full revision/tree binding, raw check/input/output/cleanup receipts and trusted overlay scope.
Challenge evidence content, not just pass booleans; reject drift, mismatches or incomplete evidence.
Run the declared runnable_scope.command, not the full suite (omitted scripts prevent collection).
Do not reconstruct a full Git tree from this partial export or treat supplied checks as independently reproduced.
Unknown returned top-five targets remain unjudged; never reinterpret labels.
"""


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    require(isinstance(value, dict), "Expected JSON object")
    return value


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_json(path: Path, value: Any, *, replace: bool = False) -> None:
    """Create immutable artifacts, or atomically replace the single authority."""
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temp.open("xb") as stream:
            temp.chmod(0o600)
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)  # No overwrite, including a dangling symlink.
            temp.unlink()
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def campaign_lock(campaign: Path) -> Iterator[None]:
    with (campaign / "lock").open("a+b") as stream:
        os.chmod(campaign / "lock", 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with recovery.controller_lock(stream.fileno()):
            yield


def git(repo: Path, *args: str, data: bytes | None = None, index: Path | None = None) -> bytes:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Private lab", "GIT_AUTHOR_EMAIL": "lab@invalid",
           "GIT_COMMITTER_NAME": "Private lab", "GIT_COMMITTER_EMAIL": "lab@invalid",
           "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
           "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00"}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(["git", "-c", "core.fsync=all", "-c", "core.fsyncMethod=fsync", "-C", str(repo), *args], input=data, env=env,
                          check=True, capture_output=True, pass_fds=recovery.lock_fds()).stdout


def revision_exists(repo: Path, revision: str) -> None:
    require(bool(re.fullmatch(r"[a-f0-9]{40}", revision)), "Exact commit required")
    require(git(repo, "rev-parse", revision + "^{commit}").decode().strip() == revision,
            "Commit identity mismatch")


def benchmark_inputs(config: dict) -> dict:
    return {key: Path(config[key]) if key in PATH_KEYS else config[key] for key in INPUT_KEYS}


def controller_identity() -> dict:
    scripts = Path(__file__).parent
    names = ("lab_experiment.py", "lab_agent.py", "lab_agent_tools.py", "lab_model_client.py",
             "lab_evaluator.py", "lab_evaluator_runtime.py", "lab_evaluator_probe.py",
             "lab_runtime.py", "lab_corpus_baseline.py")
    files = {name: file_sha(scripts / name) for name in names if (scripts / name).is_file()}
    tests = scripts.parent / "tests"
    for name in ("conftest.py", "test_public_contract.py", "test_plugin.py"):
        files["tests/" + name] = file_sha(tests / name)
    for directory in (scripts, tests):
        for path in sorted(directory.rglob("*.py")):
            files[str(path.relative_to(scripts.parent))] = file_sha(path)
    files["pyproject.toml"] = file_sha(scripts.parent / "pyproject.toml")
    from scripts.lab_check_inputs import trusted_test_inputs
    files.update(trusted_test_inputs(scripts.parent))
    return files


TOOL_FILES = {"bin/jq", "lib/libjq.so.1", "lib/libonig.so.5"}


def freeze_check_tools(target: Path, sources: dict[str, str]) -> str:
    """Copy explicit real jq inputs, recording original paths and exact bytes.

    This is an operator/controller action, never candidate execution. No host
    directories or system libraries are mounted into a worker.
    """
    require(set(sources) == TOOL_FILES, "Explicit jq and two dependency sources required")
    target.mkdir(mode=0o700)
    provenance = {}
    for name, source in sorted(sources.items()):
        path = Path(source).resolve(strict=True)
        require(path.is_file(), "Tool input must be regular")
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)
        output.chmod(0o755 if name == "bin/jq" else 0o644)
        provenance[name] = {"source": str(path), "sha256": file_sha(output)}
        require(file_sha(path) == provenance[name]["sha256"], "Tool input changed during copy")
    durable_json(target / "provenance.json", provenance)
    return check_tools_identity(target)


def check_tools_identity(root: Path) -> str:
    require(set(workspace_files(root)) == TOOL_FILES | {"provenance.json"}, "Unexpected check-tool files")
    provenance = read_json(root / "provenance.json")
    require(set(provenance) == TOOL_FILES, "Incomplete tool provenance")
    for name in TOOL_FILES:
        require(file_sha(root / name) == provenance[name]["sha256"]
                and Path(provenance[name]["source"]).is_absolute(), "Check-tool provenance mismatch")
    require((root / "bin/jq").stat().st_mode & 0o111, "jq must have executable mode")
    return snapshot(root)


def isolated_check(args: list[str], *, name: str, image: str, work: Path,
                   inputs: Path, prefix: Path) -> subprocess.CompletedProcess:
    """Create/read back/start/remove one identity-fenced, read-only check container."""
    from scripts.lab_runtime import run
    expected_env = {args[i + 1] for i, value in enumerate(args) if value == "--env"}
    expected_command = args[args.index(image) + 1:]
    disk.checkpoint()
    planned = recovery.plan_resource(args, image)
    container_id = run(args).strip()
    recovery.observed_resource(planned, container_id)
    cleanup_complete = False

    def owned_spec() -> dict:
        spec = json.loads(run(["docker", "inspect", container_id]))[0]
        require(spec["Id"] == container_id and spec["Name"] == "/" + name
                and spec["Config"]["Labels"].get("lk.lab") == "ticket-69",
                "Full-check cleanup ownership mismatch")
        return spec

    def stop_on_limit() -> None:
        spec = owned_spec()
        if spec["State"]["Running"]:
            run(["docker", "kill", container_id])
        require(owned_spec()["State"]["Running"] is False, "Full-check worker did not stop")

    try:
        spec = json.loads(run(["docker", "inspect", container_id]))[0]
        host, config = spec["HostConfig"], spec["Config"]
        mounts = {(m["Type"], m["Source"], m["Destination"], m["RW"]) for m in spec["Mounts"]}
        mounts.discard(("tmpfs", "", "/tmp", True))
        # The sealed base image may declare /opt/data as an anonymous volume;
        # override it with a tmpfs so no uninspected persistent mount is created.
        mounts.discard(("tmpfs", "", "/opt/data", True))
        require(spec["Id"] == container_id and spec["Name"] == "/" + name and spec["Image"] == image
                and config["Labels"].get("lk.lab") == "ticket-69"
                and config["User"] == f"{os.getuid()}:{os.getgid()}"
                and host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
                and host["Privileged"] is False and host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
                and "no-new-privileges:true" in host["SecurityOpt"]
                and host["Memory"] == 2147483648 and host["MemorySwap"] == 2147483648
                and host["NanoCpus"] == 2000000000 and host["PidsLimit"] == 256
                and host["RestartPolicy"]["Name"] == "no"
                and config["WorkingDir"] == "/candidate"
                and config["Entrypoint"] == ["/opt/lab-tools/bin/python"]
                and config["Cmd"] == expected_command
                and expected_env <= set(config["Env"])
                and host["Tmpfs"] == {
                    "/tmp": "rw,nosuid,nodev,size=536870912,mode=1777",
                    "/opt/data": "rw,nosuid,nodev,size=67108864,mode=1777"}
                and mounts == {("bind", str(work.resolve()), "/candidate", False),
                               ("bind", str(inputs.resolve()), "/check-inputs", False)},
                "Full-check isolation readback mismatch")
        with disk.owned_worker(stop_on_limit):
            disk.checkpoint()
            result = bounded_process(["docker", "start", "--attach", container_id], prefix=prefix,
                                     env={k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ},
                                     on_limit=stop_on_limit)
            disk.checkpoint()
        stopped = json.loads(run(["docker", "inspect", container_id]))[0]
        require(stopped["State"]["Running"] is False, "Full-check worker did not stop")
        process_exit = result.returncode
        if process_exit == 0:
            result.returncode = stopped["State"]["ExitCode"]
        # This is a check-level receipt: distinguish the effective worker result
        # from the attach CLI's raw process status, retaining both provenance facts.
        output_path = prefix.with_suffix(".output.json")
        output = read_json(output_path)
        require(output["exit_code"] == process_exit, "Attach process receipt mismatch")
        durable_json(output_path, {**output, "process_exit_code": process_exit,
                     "container_exit_code": stopped["State"]["ExitCode"],
                     "exit_code": result.returncode,
                     "exit_code_source": "stopped_container" if process_exit == 0 else "attach_process"}, replace=True)
        return result
    finally:
        try:
            owned_spec()
            run(["docker", "rm", "--force", "--volumes", container_id])
            require(not run(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"]).strip(),
                    "Full-check worker remains")
            cleanup_complete = True
        finally:
            durable_json(prefix.with_suffix(".cleanup.json"),
                         {"container_id": container_id, "name": name, "cleanup_complete": cleanup_complete})


def full_candidate_checks(*, attempt: Path, repo: Path, revision: str, image: str, name: str,
                          check_tools: Path, check_tools_sha256: str) -> dict:
    """Trusted full pytest/Ruff/mypy in a fresh network-less stopped container.

    Candidate code never executes on the controller host. Tests, scripts and
    static-check configuration come from the frozen controller, not the worker.
    The Git revision still supplies all candidate runtime package bytes.
    """
    from scripts.lab_runtime import prepare_attempt
    disk.checkpoint()
    identity_before = controller_identity()
    require(check_tools_identity(check_tools) == check_tools_sha256, "Check-tool pin mismatch")
    prepare_attempt(attempt, repo, revision)
    work = attempt / "work" / "plugin"
    inputs = attempt / "check-inputs"
    inputs.mkdir(mode=0o700)
    exact = inputs / "exact-candidate"
    shutil.copytree(work, exact)
    expected = {name: hashlib.sha256(git(repo, "cat-file", "blob", oid)).hexdigest()
                for name, (_, oid) in tracked(repo, revision).items()}
    from scripts.lab_full_check_probe import tree_identity
    require(tree_identity(exact) == expected, "Exact candidate Git export mismatch")
    shutil.copytree(check_tools, inputs / "tools")
    require(check_tools_identity(inputs / "tools") == check_tools_sha256, "Tool copy mismatch")
    trusted = Path(__file__).resolve().parents[1]
    for directory in ("tests", "scripts"):
        shutil.rmtree(work / directory, ignore_errors=True)
        shutil.copytree(trusted / directory, work / directory,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".mypy_cache"))
    shutil.copyfile(trusted / "pyproject.toml", work / "pyproject.toml")
    from scripts.lab_check_inputs import overlay_test_inputs
    trusted_inputs = overlay_test_inputs(trusted, work)
    source_before = snapshot(work)
    durable_json(inputs / "inputs.json", {"revision": revision,
                 "candidate": expected, "overlay": tree_identity(work),
                 "trusted_test_inputs": trusted_inputs, "controller": identity_before,
                 "tools": tree_identity(inputs / "tools"), "check_tools_sha256": check_tools_sha256})
    inputs_before = snapshot(inputs)
    commands = {
        "full_pytest": ["-m", "pytest", "-ra", "--tb=no", "--show-capture=no", "--disable-warnings", "--no-summary",
                        "-p", "no:cacheprovider", "-p", "scripts.lab_full_check_probe"],
        "ruff": ["-m", "ruff", "check", "--no-cache", "--output-format=concise", "."],
        "mypy": ["-m", "mypy", "--no-pretty", "--cache-dir=/tmp/mypy"],
    }
    checks = []
    for key, command in commands.items():
        check_name = name + "-" + key.replace("_", "-")
        args = ["docker", "create", "--name", check_name,
                "--label", "lk.lab=ticket-69", "--network", "none", "--read-only",
                "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--pids-limit", "256",
                "--memory", "2g", "--memory-swap", "2g", "--cpus", "2", "--restart", "no",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=536870912,mode=1777",
                "--tmpfs", "/opt/data:rw,nosuid,nodev,size=67108864,mode=1777",
                "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/candidate",
                "--env", "CI=1", "--env", "LD_LIBRARY_PATH=/check-inputs/tools/lib",
                "--env", "PATH=/check-inputs/tools/bin:/opt/lab-tools/bin:/opt/hermes/bin:/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                "--mount", f"type=bind,src={inputs.resolve()},dst=/check-inputs,readonly",
                "--mount", f"type=bind,src={work.resolve()},dst=/candidate,readonly",
                "--workdir", "/candidate", "--entrypoint", "/opt/lab-tools/bin/python", image, *command]
        prefix = attempt / key  # Private controller output, outside both candidate binds.
        result = isolated_check(args, name=check_name, image=image, work=work, inputs=inputs, prefix=prefix)
        from scripts.lab_review_evidence import receipt_files
        checks.append({"name": key, "command": ["/opt/lab-tools/bin/python", *command],
                       "evidence_files": receipt_files(attempt, key),
                       "passed": result.returncode == 0, "returncode": result.returncode,
                       "output_receipt": prefix.with_suffix(".output.json").name,
                       "cleanup_receipt": prefix.with_suffix(".cleanup.json").name})
    require(snapshot(work) == source_before and snapshot(inputs) == inputs_before, "Full-check source drift")
    require(check_tools_identity(check_tools) == check_tools_sha256, "Check-tool pin drift")
    require(controller_identity() == identity_before, "Full-check controller drift")
    result_report = {"revision": revision, "image": image, "controller": sha(identity_before),
                     "checks": checks, "passed": all(c["passed"] for c in checks),
                     "inputs_sha256": inputs_before, "inputs_manifest_sha256": file_sha(inputs / "inputs.json"),
                     "check_tools_sha256": check_tools_sha256,
                     "exact_candidate_sha256": snapshot(exact), "overlay_sha256": source_before,
                     "cleanup_complete": True}
    durable_json(attempt / "checks.json", result_report)
    return result_report


def unknown_target_clearance(campaign: Path, binding: dict) -> bool:
    """Explicit operator-owned symmetric source-review receipt; absent means no.

    This is not label rewriting or automatic adjudication. Bind clearance to both
    aggregate executions and the exact immutable candidate/config/controller.
    """
    path = campaign / "clearances" / (sha(binding) + ".json")
    if not path.exists():
        return False
    require(not path.is_symlink() and stat.S_ISREG(path.lstat().st_mode), "Unsafe clearance receipt")
    receipt = read_json(path)
    return (receipt.get("binding") == binding and receipt.get("symmetric_source_review") is True
            and receipt.get("unknown_targets_cleared") is True and bool(receipt.get("rationale")))


def validate_config(config: dict, loader: Callable[..., dict] = load_benchmark) -> dict:
    require(config.get("schema") == SCHEMA, "Unsupported campaign")
    require(config.get("initial_incumbent") == INITIAL_INCUMBENT, "Original incumbent must be explicit")
    from scripts.lab_agent import IMAGE
    validate_pin(config["image"])
    require(config["image"] == IMAGE, "Campaign requires the fixed agent image")
    require(check_tools_identity(Path(config["check_tools"])) == config["check_tools_sha256"],
            "Check-tool pin mismatch")
    for key in ("frozen_sha256", "policy_sha256", "development_sha256", "manifest_sha256"):
        require(bool(re.fullmatch(r"[a-f0-9]{64}", config[key])), "Invalid hash pin")
    return loader(**benchmark_inputs(config))


def init_campaign(campaign: Path, config: dict, *, loader: Callable[..., dict] = load_benchmark) -> dict:
    """Explicit initialization; no default image, HEAD, benchmark or incumbent."""
    campaign = campaign.resolve()
    config = dict(config)
    for key in PATH_KEYS | {"repo", "check_tools"}:
        config[key] = str(Path(config[key]).resolve(strict=True))
    for repository in (Path(__file__).resolve().parents[1], Path(config["repo"])):
        require(not campaign.is_relative_to(repository), "Campaign must be outside the repository")
    validate_config(config, loader)
    revision_exists(Path(config["repo"]), config["initial_incumbent"])
    campaign.mkdir(mode=0o700)  # Never reuse a partially initialized campaign.
    (campaign / "attempts").mkdir(mode=0o700)
    shutil.copytree(config["check_tools"], campaign / "check-tools")
    config["check_tools"] = str(campaign / "check-tools")
    require(check_tools_identity(Path(config["check_tools"])) == config["check_tools_sha256"],
            "Frozen check-tool copy mismatch")
    durable_json(campaign / "config.json", config)
    # Private durable objects survive disposal of the implementation workspace.
    git(Path(config["repo"]), "clone", "--bare", "--no-hardlinks", "--", config["repo"], str(campaign / "repo.git"))
    git(campaign / "repo.git", "remote", "remove", "origin")
    manifest = {"schema": SCHEMA, "config_sha256": sha(config),
                "incumbent": config["initial_incumbent"], "reports": [], "pending": None,
                "paused": False}
    durable_json(campaign / "manifest.json", manifest)
    fsync_directory(campaign.parent)
    return manifest


def tracked(repo: Path, revision: str) -> dict[str, tuple[str, str]]:
    entries = {}
    for row in git(repo, "ls-tree", "-rz", revision).split(b"\0"):
        if row:
            meta, name = row.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            require(kind == "blob" and mode in {"100644", "100755"}, "Unsupported incumbent tree member")
            entries[name.decode()] = (mode, oid)
    return entries


def editable(name: str) -> bool:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or any(p.startswith(".") for p in path.parts):
        return False
    return ((path.parts[0] == "hermes_local_knowledge" and path.suffix == ".py")
            or (path.parts[0] == "examples" and path.suffix in {".json", ".yaml", ".yml", ".toml"}))


def workspace_files(workspace: Path) -> dict[str, bytes]:
    from scripts.lab_workspace import read_files
    return read_files(workspace)


def snapshot(workspace: Path) -> str:
    from scripts.lab_workspace import file_hash, inventory
    return sha({name: file_hash(path) for name, path in inventory(workspace).items()})


def candidate_commit(repo: Path, previous: str, workspace: Path, attempt: Path) -> tuple[str, list[dict]]:
    """Import only validated regular files; retain every noneditable incumbent byte."""
    before = tracked(repo, previous)
    files = workspace_files(workspace)
    changes = []
    index = attempt / "candidate.index"
    git(repo, "read-tree", previous, index=index)
    for name in sorted(set(files) | {n for n in before if editable(n)}):
        old = before.get(name)
        content = files.get(name)
        if not editable(name):
            require(old is not None and content == git(repo, "cat-file", "blob", old[1]),
                    "Forbidden or unapproved worker path change")
            continue
        oid = git(repo, "hash-object", "-w", "--stdin", data=content).decode().strip() if content is not None else None
        if (old[1] if old else None) == oid:
            continue
        changes.append({"path": name, "operation": "delete" if oid is None else "modify" if old else "add"})
        if oid is None:
            git(repo, "update-index", "--index-info", data=("0 " + "0" * 40 + "\t" + name + "\n").encode(), index=index)
        else:
            git(repo, "update-index", "--add", "--cacheinfo", old[0] if old else "100644", oid, name, index=index)
    if not changes:
        return previous, []
    tree = git(repo, "write-tree", index=index).decode().strip()
    revision = git(repo, "commit-tree", tree, "-p", previous, data=b"Private lab candidate\n").decode().strip()
    # Anchor candidate objects against GC, without modifying the incumbent authority.
    git(repo, "update-ref", "refs/lab/candidates/" + attempt.name, revision)
    return revision, changes


def session_valid(result: dict, role: str, revision: str) -> None:
    require(result.get("complete") is True and result.get("revision") == revision
            and result.get("role") == role and all(result.get(k) == v for k, v in MODEL.items()),
            "Incomplete or mismatched model session")
    require(isinstance(result.get("session_id"), str) and bool(result["session_id"]), "Session ID missing")
    require(isinstance(result.get("final"), dict) and isinstance(result.get("usage"), list), "Malformed session result")
    require(bool(result.get("source_identity")), "Session source identity missing")


def validate_session_source(result: dict, expected: Path) -> None:
    require(Path(result["workspace"]).absolute() == expected.absolute(), "Unexpected session workspace")
    files = {name: hashlib.sha256(data).hexdigest()
             for name, data in workspace_files(expected).items()}
    require(result["source_identity"] == {
        "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), "files": files},
        "Session source identity mismatch")


def safe_usage(sessions: list[dict]) -> list[dict]:
    """Do not invent usage; provider objects stay private, history gets numeric totals."""
    return [{"role": s["role"], "session_id": s["session_id"], **MODEL,
             "usage": [{k: v for k, v in item.items() if k in {
                 "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
                 "cache_read_input_tokens", "cache_creation_input_tokens", "cost"}
                 and type(v) in (int, float)} for item in s["usage"] if isinstance(item, dict)],
             "cleanup_complete": s.get("cleanup_complete") if type(s.get("cleanup_complete")) is bool else None,
             "usage_status": "provider_reported" if any(
                 isinstance(item, dict) and any(type(v) in (int, float) for v in item.values())
                 for item in s["usage"]) else "unknown"} for s in sessions]


def recover_usage(attempt: Path, sessions: list[dict]) -> list[dict]:
    """Read trusted host-side partial receipts after a failed model session."""
    result = list(sessions)
    # Prefer complete controller/session results over partial status. Keep distinct
    # old review sessions and provider-reported usage across verification generations.
    directories = recovery.session_directories(attempt)
    private = [directory / "private" for directory in directories]
    paths = [attempt / "implementation-result.json",
             *(directory.parent / "review-result.json" for directory in directories if directory.name == "review"),
             *(directory / name for name in ("result.json", "status.json", "session.json") for directory in private)]
    for path in paths:
        if not path.is_file():
            continue
        status = read_json(path)
        if status.get("role") not in {"implementation", "review"}:
            continue
        if any(s["session_id"] == status["session_id"] for s in result):
            continue
        if "usage" not in status:
            status["usage"] = [read_json(p).get("usage", {"available": False})
                               for p in path.parent.glob("response-*.json")]
        if isinstance(status.get("usage"), list):
            result.append(status)
    return result


def safe_aggregate(report: dict) -> dict:
    result = {k: report[k] for k in ("revision", "image", "benchmark_identity", "integrity", "trusted_tests",
              "official_integration", "complete", "errors", "warm_p95_seconds", "scored_cases") if k in report}
    for split in ("development", "validation"):
        result[split] = {k: report[split][k] for k in ("count", "hit_at_5", "hit_at_1", "origin_hit_at_5")}
        require(all(type(v) is int for v in result[split].values()), "Aggregate counts must be integers")
    return result


def history_projection(report: dict) -> dict:
    """Safe handoff includes measured aggregates, never cases or returned bodies."""
    keys = ("attempt_id", "hypothesis", "strategy", "strategy_id", "related_attempts", "revisit_reason",
            "actual_changes", "revision", "previous_incumbent", "outcome", "lessons", "checks", "usage",
            "delivery_validation")
    result = {key: report[key] for key in keys if key in report}
    review = report.get("independent_review")
    if isinstance(review, dict):
        from scripts.lab_corpus import scrub_content
        findings = review.get("findings", [])
        prose = []
        for finding in findings[:20] if isinstance(findings, list) else []:
            if isinstance(finding, dict):
                # Reviewer schemas permit structured findings. Keep actionable
                # prose fields, never arbitrary nested evidence/provider bodies.
                finding = "; ".join(f"{key}: {finding[key]}" for key in (
                    "severity", "summary", "message", "reason", "description", "recommendation", "path")
                    if isinstance(finding.get(key), str) and len(finding[key]) <= 16000)
            if isinstance(finding, str) and finding and len(finding) <= 16000:
                prose.append(scrub_content(finding)[0][:1000])
        result["independent_review"] = {
            "approved": review.get("approved") is True,
            "revision": review.get("revision"),
            # Scrub before truncating so a cut cannot hide credential syntax.
            "findings": prose,
        }
    measured = report.get("measured_outcomes", {})
    result["measured_outcomes"] = {role: safe_aggregate(measured[role])
        for role in ("incumbent", "candidate") if role in measured}
    return result


def finalized_reports(campaign: Path, manifest: dict) -> list[dict]:
    reports = []
    for pointer in manifest["reports"]:
        path = campaign / "attempts" / pointer["attempt_id"] / "report.json"
        require(file_sha(path) == pointer["sha256"], "Final report drift")
        reports.append(read_json(path))
    return reports


def publish(campaign: Path, expected: dict, report: dict, *, paused: bool = False,
            hold_pending: bool = False, recovery_pause: bool = False) -> dict:
    require(read_json(campaign / "manifest.json") == expected, "Campaign compare-and-swap failed")
    path = campaign / "attempts" / report["attempt_id"] / "report.json"
    durable_json(path, report)
    require(read_json(campaign / "manifest.json") == expected, "Campaign compare-and-swap failed")
    updated = {**expected, "pending": expected["pending"] if hold_pending else None, "paused": paused,
               "incumbent": report["revision"] if report["outcome"] == "accepted" else expected["incumbent"],
               "reports": [*expected["reports"], {"attempt_id": report["attempt_id"], "sha256": file_sha(path)}]}
    if not paused:
        updated.pop("pause_source", None)
    elif recovery_pause and not expected["paused"]:
        updated["pause_source"] = "recovery"
    durable_json(campaign / "manifest.json", updated, replace=True)
    require(read_json(campaign / "manifest.json") == updated, "Manifest readback failed")
    return updated


def archive_report(attempt: Path) -> None:
    """Preserve a report-before-manifest orphan, never install its old gates."""
    path = attempt / "report.json"
    archive = attempt / ("report.pre-recovery-" + file_sha(path) + ".json")
    if archive.exists():
        require(archive.read_bytes() == path.read_bytes(), "Recovery archive conflict")
        path.unlink()
    else:
        os.rename(path, archive)
    fsync_directory(attempt)


def recovery_status(campaign: Path) -> dict:
    """Read-only durable projection; does not acquire/create lock files or inspect Docker."""
    manifest = read_json(campaign / "manifest.json")
    result = dict(manifest)
    attempt_id = manifest["pending"] or (manifest["reports"][-1]["attempt_id"] if manifest["reports"] else None)
    if attempt_id:
        attempt = campaign / "attempts" / attempt_id
        result["checkpoint"] = read_json(attempt / "checkpoint.json") if (attempt / "checkpoint.json").exists() else None
        result["recovery_report"] = read_json(attempt / "recovery-report.json") if (attempt / "recovery-report.json").exists() else None
    result["recovery"] = read_json(campaign / "recovery-alert.json") if (campaign / "recovery-alert.json").exists() else None
    return result


def pause_recovery(campaign: Path, reason: str) -> None:
    # The monitor owns the same action lock: either this pause commits first and
    # a subsequent disk pause wins, or an existing disk failure rejects this write.
    with disk.action():
        current = read_json(campaign / "manifest.json")
        alert = {"kind": "lab_recovery_paused", "reason": reason,
                 "attempt_id": current["pending"], "action": "Inspect exact attempt receipts; no automatic model retry."}
        durable_json(campaign / "recovery-alert.json", alert, replace=True)
        durable_json(campaign / "manifest.json", {**current, "paused": True, "pause_source": "recovery"}, replace=True)
        require(read_json(campaign / "recovery-alert.json") == alert
                and read_json(campaign / "manifest.json")["paused"] is True, "Recovery pause readback failed")


def recovered_implementation(attempt: Path, config: dict) -> dict | None:
    """A final response is usable only after quiescence, cleanup and source validation.

    No conversation/tool replay: incomplete settled sessions are interrupted reports.
    """
    path = attempt / "implementation-result.json"
    if path.exists():
        result = read_json(path)
        validate_session_source(result, attempt / "implementation/work/plugin")
        return result
    root = attempt / "implementation"
    private = root / "private"
    result_path = private / "result.json"
    if result_path.exists():
        result = read_json(result_path)
        require(result.get("cleanup_complete") is True, "Recovered result cleanup incomplete")
        validate_session_source(result, root / "work/plugin")
        durable_json(path, result)
        return result
    session_path = private / "session.json"
    if not session_path.exists():
        return None
    responses = sorted(private.glob("response-*.json"), key=lambda p: int(p.stem.split("-")[1]))
    if not responses:
        return None
    response = read_json(responses[-1])
    message = response["message"]
    if message.get("tool_calls"):
        return None
    from scripts import lab_agent as agent
    metadata = read_json(session_path)
    final = agent.validate_final(json.loads(message["content"]), "implementation")
    # Resources were inspected and settled before this helper can execute.
    agent.ownership(root, config["image"], "lk-lab-" + attempt.name + "-recover", f"{os.getuid()}:{os.getgid()}")
    result = {**metadata, "workspace": str(root / "work/plugin"), "final": final,
              "usage": [read_json(p).get("usage", {"available": False}) for p in responses],
              "complete": True, "cleanup_complete": True, "source_identity": agent.identity(root / "work/plugin")}
    validate_session_source(result, root / "work/plugin")
    durable_json(path, result)
    return result


def run_experiment(campaign: Path, *, session: Callable[..., dict] | None = None,
                   evaluator: Callable[..., dict] | None = None,
                   checks: Callable[..., dict] = full_candidate_checks,
                   clearance: Callable[[Path, dict], bool] = unknown_target_clearance,
                   loader: Callable[..., dict] = load_benchmark,
                   recover_only: bool = False) -> dict:
    """Run or recover one attempt under the campaign's non-expiring flock."""
    campaign = campaign.resolve(strict=True)
    with campaign_lock(campaign):
        return run_under_existing_lock(campaign, session=session, evaluator=evaluator,
                                       checks=checks, clearance=clearance, loader=loader,
                                       recover_only=recover_only)


def run_under_existing_lock(campaign: Path, *, session: Callable[..., dict] | None = None,
                   evaluator: Callable[..., dict] | None = None,
                   checks: Callable[..., dict] = full_candidate_checks,
                   clearance: Callable[[Path, dict], bool] = unknown_target_clearance,
                   loader: Callable[..., dict] = load_benchmark,
                   recover_only: bool = False) -> dict:
    """Run or recover one attempt. Injection is Python-test-only, never CLI input."""
    require(bool(recovery.lock_fds()), "Existing campaign lock required")
    campaign = campaign.resolve(strict=True)
    expected_lock = (campaign / "lock").stat()
    held_lock = os.fstat(recovery.lock_fds()[0])
    require((held_lock.st_dev, held_lock.st_ino) == (expected_lock.st_dev, expected_lock.st_ino),
            "Existing lock belongs to another campaign")
    if session is None:
        from scripts.lab_agent import run_session
        session = run_session
    if evaluator is None:
        from scripts.lab_evaluator_runtime import evaluate_revision
        evaluator = evaluate_revision
    manifest = read_json(campaign / "manifest.json")
    config = read_json(campaign / "config.json")
    if recover_only and manifest["pending"] is None:
        return recovery_status(campaign)
    require(not manifest["paused"] or (recover_only and manifest["pending"]
            and manifest.get("pause_source") in {"recovery", "disk"}),
            "Campaign paused or pending; explicit recovery required")
    if sha(config) != manifest["config_sha256"]:
        if manifest["pending"]:
            pause_recovery(campaign, "configuration_drift")
        raise ValueError("Campaign configuration drift")
    def pause_disk(alert: dict) -> None:
        current = read_json(campaign / "manifest.json")
        # Disk failure supersedes recovery, but must not grant permission to
        # bypass a pre-existing manual/auth pause (which has no recovery owner).
        if not current["paused"] or current.get("pause_source") in {"recovery", "disk"}:
            current["pause_source"] = "disk"
        durable_json(campaign / "manifest.json", {**current, "paused": True}, replace=True)
        durable_json(campaign / "disk-alert.json", alert, replace=True)
        require(read_json(campaign / "manifest.json")["paused"] is True,
                "Disk pause readback failed")
        require(read_json(campaign / "disk-alert.json") == alert, "Disk alert readback failed")
        print(json.dumps(alert, sort_keys=True), file=sys.stderr, flush=True)

    with disk.DiskGuard(campaign, pause_disk) as guard, ExitStack() as lifecycle:
        if recover_only and manifest["paused"] and manifest.get("pause_source") == "disk":
            # __enter__ sampled fresh space successfully. Explicit recovery may
            # now reconcile this same pending attempt, never admit a new one.
            with guard.action():
                manifest = {**manifest, "pause_source": "recovery"}
                durable_json(campaign / "manifest.json", manifest, replace=True)
                require(read_json(campaign / "manifest.json") == manifest, "Disk reconciliation readback failed")
        # Unknown directories predate the manifest checkpoint: never guess ownership.
        finalized = {p["attempt_id"] for p in manifest["reports"]}
        known = finalized | ({manifest["pending"]} if manifest["pending"] else set())
        require(all(p.name in known for p in (campaign / "attempts").iterdir()), "Unreconciled attempt directory")
        try:
            benchmark = validate_config(config, loader)
            identity = controller_identity()
        except Exception:
            if manifest["pending"] and not guard.failed:
                pause_recovery(campaign, "campaign_inputs_unavailable_or_changed")
            raise
        previous = manifest["incumbent"]
        repo = campaign / "repo.git"
        revision_exists(repo, previous)
        resuming = manifest["pending"] is not None
        attempt_id = manifest["pending"] or uuid.uuid4().hex
        attempt = campaign / "attempts" / attempt_id
        guard.check()
        binding = {"config": sha(config), "controller": sha(identity), "image": config["image"],
                   "benchmark": config["composite_id"], "previous_incumbent": previous}
        reports = finalized_reports(campaign, manifest)
        implementation = None
        generation = 0
        if resuming:
            try:
                require((attempt / "checkpoint.json").exists(), "Missing recovery checkpoint")
                if read_json(attempt / "attempt.json") != {"attempt_id": attempt_id, **binding, **MODEL}:
                    raise recovery.RecoveryPaused("controller_or_campaign_drift")
                guard.check()
                resources = recovery.settle_resources(attempt)
                guard.check()
                completion = recovery.session_completion(attempt)
                durable_json(attempt / "recovery-report.json", {"attempt_id": attempt_id,
                             "resources": resources, "model_completion": completion,
                             "state": "quiescent"}, replace=True)
                # A published error that held pending may now be conclusively settled.
                if attempt_id in finalized:
                    require(any(r["attempt_id"] == attempt_id and r["outcome"] != "accepted" for r in reports),
                            "Invalid pending publication")
                    durable_json(attempt / "recovery-report.json", {"attempt_id": attempt_id,
                                 "state": "finalized_report_settled", "resources": resources,
                                 "report_sha256": file_sha(attempt / "report.json")}, replace=True)
                    updated = {**manifest, "pending": None, "paused": False}
                    updated.pop("pause_source", None)
                    with guard.action():
                        durable_json(campaign / "manifest.json", updated, replace=True)
                        require(read_json(campaign / "manifest.json") == updated, "Recovery readback failed")
                    return next(r for r in reports if r["attempt_id"] == attempt_id)
                with recovery.resources(attempt):
                    try:
                        guard.check()
                        implementation = recovered_implementation(attempt, config) if completion == "settled" else None
                        guard.check()
                    except Exception:
                        raise recovery.RecoveryPaused("implementation_checkpoint_invalid") from None
                generations = [int(p.name.split("-")[1]) for p in attempt.glob("generation-*")
                               if p.name.split("-")[1].isdigit()]
                generation = max([read_json(attempt / "checkpoint.json")["generation"], *generations]) + 1
                if implementation is not None:
                    session_valid(implementation, "implementation", previous)
                    if (attempt / "candidate.json").exists():
                        pinned = read_json(attempt / "candidate.json")
                        candidate, _ = candidate_commit(repo, previous, Path(implementation["workspace"]), attempt)
                        if pinned != {**binding, "revision": candidate,
                                "tree": git(repo, "rev-parse", candidate + "^{tree}").decode().strip()}:
                            raise recovery.RecoveryPaused("candidate_revision_drift")
                if implementation is None:
                    # No transcript replay. Real settled partial sessions can be finalized;
                    # missing session identity is not proof a model call never happened.
                    require((attempt / "implementation/private/session.json").exists(),
                            "Campaign paused or pending; session ownership unknown")
                    draft = read_json(attempt / "draft-report.json")
                    draft.update(outcome="interrupted", reconciliation_required=False,
                                 usage=safe_usage(recover_usage(attempt, [])))
                    if (attempt / "report.json").exists():
                        archive_report(attempt)
                    with guard.action():
                        publish(campaign, manifest, draft)
                    durable_json(attempt / "recovery-report.json", {"attempt_id": attempt_id,
                                 "state": "interrupted_finalized", "resources": resources,
                                 "report_sha256": file_sha(attempt / "report.json")}, replace=True)
                    return draft
                orphan = attempt / "report.json"
                if orphan.exists():
                    archive_report(attempt)
            except Exception as error:
                # Recovery must not relabel/clear an independent disk pause.
                if guard.failed:
                    guard.check()
                reason = str(error) if isinstance(error, recovery.RecoveryPaused) else "recovery_evidence_uncertain"
                pause_recovery(campaign, reason)
                raise ValueError("Campaign paused or pending; " + reason) from None
        else:
            attempt.mkdir(mode=0o700)
            fsync_directory(attempt.parent)
            durable_json(attempt / "attempt.json", {"attempt_id": attempt_id, **binding, **MODEL})
            durable_json(attempt / "history.json", {"reports": [history_projection(r) for r in reports]})
        with guard.action():
            pending = {**manifest, "pending": attempt_id, "paused": False}
            pending.pop("pause_source", None)
            durable_json(campaign / "manifest.json", pending, replace=True)
        report: dict[str, Any] = {"attempt_id": attempt_id, **binding, "revision": previous,
            "hypothesis": None, "strategy": None, "related_attempts": [], "revisit_reason": None,
            "actual_changes": [], "checks": [], "measured_outcomes": {}, "outcome": "error",
            "lessons": [], "usage": [], "semantic_caveat": "Unknown returned top-five targets remain unjudged."}
        lifecycle.enter_context(recovery.resources(attempt))
        generation_root = attempt if generation == 0 else attempt / ("generation-" + str(generation))
        if generation:
            generation_root.mkdir(mode=0o700)
        report["verification_generation"] = generation
        sessions: list[dict] = recover_usage(attempt, []) if resuming else []
        inflight: tuple[str, str] | None = None

        def checkpoint(phase: str) -> None:
            guard.check()
            durable_json(attempt / "draft-report.json", report, replace=True)
            durable_json(attempt / "checkpoint.json", {"phase": phase, "generation": generation,
                         "revision": report["revision"]}, replace=True)

        checkpoint("created")
        worker_prefix = "lk-lab-" + attempt_id + ("-g" + str(generation) if generation else "")
        common = {"repo": repo, "corpus": Path(config["corpus"]), "development": Path(config["development"]),
                  "history": attempt / "history.json", "image": config["image"]}

        def pins_unchanged() -> None:
            guard.check()
            require(read_json(campaign / "config.json") == config and controller_identity() == identity,
                    "Controller/config evidence became stale")
            validate_config(config, loader)

        def measured(revision: str, role: str) -> dict:
            nonlocal inflight
            pins_unchanged()
            inflight = ("evaluation", role)
            checkpoint("evaluation-" + role)
            result = evaluator(attempt=generation_root / ("evaluation-" + role), repo=repo, revision=revision,
                               corpus=Path(config["corpus"]), image=config["image"],
                               name=worker_prefix + "-" + role, benchmark=benchmark)
            inflight = None
            require(result.get("revision") == revision and result.get("image") == config["image"]
                    and result.get("benchmark_identity") == config["composite_id"], "Stale evaluation identity")
            pins_unchanged()
            return safe_aggregate(result)

        try:
            guard.check()
            pins_unchanged()
            if implementation is None:
                inflight = ("session", "implementation")
                checkpoint("implementation")
                implementation = session(attempt=attempt / "implementation", revision=previous,
                    name="lk-lab-" + attempt_id + "-impl", role="implementation", task=IMPLEMENT_TASK, **common)
                durable_json(attempt / "implementation-result.json", implementation)
            session_valid(implementation, "implementation", previous)
            if not any(s["session_id"] == implementation["session_id"] for s in sessions):
                sessions.append(implementation)
            guard.check()
            inflight = None
            validate_session_source(implementation, attempt / "implementation/work/plugin")
            final = implementation["final"]
            for key in ("hypothesis", "strategy", "related_attempts", "revisit_reason", "lessons"):
                report[key] = final[key]
            require(isinstance(final["strategy"], str) and bool(final["strategy"].strip()), "Strategy missing")
            require(type(final["no_change"]) is bool, "Explicit no-change decision required")
            report["strategy_id"] = sha(" ".join(final["strategy"].lower().split()))
            failed = [r for r in reports if r.get("strategy_id") == report["strategy_id"] and r["outcome"] == "rejected"]
            require(not failed or final["no_change"] or bool(str(final["revisit_reason"] or "").strip()),
                    "Repeated failed strategy needs a new reason or no-change decision")
            workspace = Path(implementation["workspace"])
            work_identity = snapshot(workspace)
            revision, changes = candidate_commit(repo, previous, workspace, attempt)
            report.update(revision=revision, actual_changes=changes)
            if final["no_change"] != (not changes):
                report["delivery_validation"] = {
                    "passed": False, "reason": "no_change_imported_edits_mismatch",
                    "declared_no_change": final["no_change"], "imported_edits": bool(changes)}
                raise ValueError("Candidate delivery mismatch: no_change contradicts imported edits")
            exact_binding = {**binding, "revision": revision, "tree": git(repo, "rev-parse", revision + "^{tree}").decode().strip()}
            if (attempt / "candidate.json").exists():
                require(read_json(attempt / "candidate.json") == exact_binding, "Recovered candidate drift")
            else:
                durable_json(attempt / "candidate.json", exact_binding)
            checkpoint("candidate")
            old = measured(previous, "incumbent")
            report["measured_outcomes"]["incumbent"] = old
            new = measured(revision, "candidate")
            report["measured_outcomes"]["candidate"] = new
            decision = verdict(old, new)
            report["measured_outcomes"]["score_verdict"] = decision
            report["checks"] = [{"name": gate, "passed": new.get(gate) is True, "revision": revision}
                                for gate in ("integrity", "trusted_tests", "official_integration", "complete")]
            guard.check()
            inflight = ("checks", "full-checks")
            checkpoint("full-checks")
            full_checks = checks(attempt=generation_root / "full-checks", repo=repo, revision=revision,
                                 image=config["image"], name=worker_prefix + "-checks",
                                 check_tools=Path(config["check_tools"]),
                                 check_tools_sha256=config["check_tools_sha256"])
            guard.check()
            inflight = None
            require(full_checks.get("revision") == revision and full_checks.get("image") == config["image"]
                    and full_checks.get("controller") == sha(identity), "Stale full-check evidence")
            actual_checks = full_checks.get("checks", [])
            full_passed = (full_checks.get("passed") is True
                           and {c["name"] for c in actual_checks} == {"full_pytest", "ruff", "mypy"}
                           and all(c.get("passed") is True for c in actual_checks))
            report["checks"].extend({**c, "revision": revision} for c in actual_checks)
            gate_binding = {**exact_binding, "verification_generation": generation,
                            "checks_sha256": sha(full_checks)}
            clearance_binding = {**gate_binding, "measured_outcomes_sha256": sha(report["measured_outcomes"])}
            cleared = clearance(campaign, clearance_binding) if decision["score_eligible"] else False
            report["unknown_target_gate"] = {"cleared": cleared, "binding": clearance_binding}
            # Reviewer sees aggregate integers and actual checks, never cases or raw responses.
            from scripts import lab_review_evidence as review_evidence
            packet = review_evidence.build(repo=repo, revision=revision, binding=gate_binding, report=report,
                checks_root=generation_root / "full-checks", proof=generation_root / "review-proof")
            durable_json(generation_root / "review-evidence.json", packet)
            pins_unchanged()
            require(snapshot(workspace) == work_identity, "Candidate changed after execution")
            inflight = ("session", "review")
            checkpoint("review")
            review = session(attempt=generation_root / "review", revision=revision,
                name=worker_prefix + "-review", role="review", task=REVIEW_TASK,
                evidence=generation_root / "review-evidence.json", **common)
            durable_json(generation_root / "review-result.json", review)
            session_valid(review, "review", revision)
            sessions.append(review)
            guard.check()
            inflight = None
            validate_session_source(review, generation_root / "review/work/plugin")
            require(review["session_id"] != implementation["session_id"], "Review must be independent")
            # The frozen Git object is authoritative; nevertheless fail on edits to either export.
            review_commit, review_changes = candidate_commit(repo, revision, Path(review["workspace"]), generation_root / "review")
            require(review_commit == revision and not review_changes, "Reviewer source changed")
            require(snapshot(workspace) == work_identity, "Implementation source changed after gates")
            pins_unchanged()
            review_final = review["final"]
            approved = all(review_final.get(k) is True for k in (
                "approved", "shortcuts_checked", "compatibility_checked", "report_checked"))
            report["independent_review"] = {"session_id": review["session_id"], "approved": approved,
                "revision": revision, "binding_sha256": sha(gate_binding), "findings": review_final.get("findings", [])}
            report["outcome"] = ("no_change" if not changes else "accepted"
                                 if decision["score_eligible"] and approved and full_passed and cleared else "rejected")
            report["usage"] = safe_usage(sessions)
            checkpoint("publication")
            with guard.action():
                publish(campaign, pending, report)
            if resuming:
                durable_json(attempt / "recovery-report.json", {"attempt_id": attempt_id,
                             "state": "complete", "verification_generation": generation,
                             "report_sha256": file_sha(attempt / "report.json")}, replace=True)
            return report
        except Exception as error:
            # Never put arbitrary candidate/provider exception text into safe reports.
            report["error_class"] = type(error).__name__
            report["outcome"] = "error"
            report["usage"] = safe_usage(recover_usage(attempt, sessions))
            paused = guard.failed or bool(re.search(r"auth|quota|modelpaused|ratelimit|credential", type(error).__name__, re.I))
            report["paused"] = paused
            uncertain = inflight is not None
            ambiguous_model_pause = False
            if inflight is not None:
                kind, role = inflight
                if kind == "session":
                    # Local cleanup is not proof a dispatched provider call
                    # completed. Reuse recovery's controller-owned receipt scan.
                    status = (attempt if role == "implementation" else generation_root) / role / "private/status.json"
                    uncertain = not (status.is_file() and read_json(status).get("cleanup_complete") is True)
                    try:
                        recovery.session_completion(attempt)
                    except recovery.RecoveryPaused as reason:
                        uncertain = True
                        from scripts.lab_model_client import ModelPaused
                        # The real sender masks transport loss as ModelPaused.
                        # Only an unanswered durable wire makes that pause
                        # recovery-owned, not preflight/schema/auth exceptions.
                        ambiguous_model_pause = (type(error) is ModelPaused
                                                 and str(reason) == "model_completion_unknown")
                    except Exception:
                        uncertain = True
                elif kind == "evaluation":
                    cleanup = generation_root / ("evaluation-" + role) / "cleanup.json"
                    receipt = read_json(cleanup) if cleanup.is_file() else {}
                    uncertain = not (receipt.get("worker_removed") is True
                                     and receipt.get("ownership_restored", True) is True)
                elif kind == "checks":
                    cleanup = generation_root / "full-checks/checks.json"
                    uncertain = not (cleanup.is_file() and read_json(cleanup).get("cleanup_complete") is True)
            report["reconciliation_required"] = uncertain
            recovery_paused = uncertain and (not paused or ambiguous_model_pause)
            paused = paused or uncertain
            if not (attempt / "report.json").exists():
                with guard.lock:
                    report["paused"] = paused or guard.failed
                    publish(campaign, read_json(campaign / "manifest.json") if guard.failed else pending,
                            report, paused=paused or guard.failed, hold_pending=uncertain, recovery_pause=recovery_paused)
            raise
        except BaseException as error:
            report.update(outcome="interrupted", error_class=type(error).__name__,
                          usage=safe_usage(recover_usage(attempt, sessions)), reconciliation_required=True)
            if not (attempt / "interrupted.json").exists():
                durable_json(attempt / "interrupted.json", report)
            raise
        # Interruption/process death deliberately leaves pending intact.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "run", "recover", "status"))
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="Required only by init; explicit pinned private JSON")
    args = parser.parse_args()
    if args.command == "init":
        require(args.config is not None, "init requires --config")
        output = init_campaign(args.campaign, read_json(args.config))
    elif args.command == "status":
        output = recovery_status(args.campaign)
    else:
        result = run_experiment(args.campaign, recover_only=args.command == "recover")
        output = history_projection(result) if "attempt_id" in result else result
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
