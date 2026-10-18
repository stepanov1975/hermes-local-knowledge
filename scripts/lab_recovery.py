"""Private restart receipts and exact-resource inspection, never a scheduler.

The campaign flock owns this context. No leases, age-based decisions, provider
retries or running-worker cleanup occur during recovery.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Iterator
import uuid

_CURRENT: ContextVar[Path | None] = ContextVar("lab_recovery_attempt", default=None)
_LOCK: ContextVar[tuple[int, ...]] = ContextVar("lab_controller_lock", default=())


def lock_fds() -> tuple[int, ...]:
    # Launch clients retain the same flock while a controller is killed. This
    # fences create/start/exec still in progress, not only a known container ID.
    return _LOCK.get()


@contextmanager
def controller_lock(fd: int) -> Iterator[None]:
    token = _LOCK.set((fd,))
    try:
        yield
    finally:
        _LOCK.reset(token)


class RecoveryPaused(ValueError):
    """Fixed structural reason only; never candidate/provider exception text."""


def atomic_json(path: Path, value: Any, *, replace: bool = False) -> None:
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temp.open("xb") as stream:
            temp.chmod(0o600)
            stream.write((json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)
            temp.unlink()
        sync_directory(path.parent)
        root = _CURRENT.get()
        if root is not None and root in path.parents:
            # Newly exported session/private directories must survive host restart,
            # not only a process kill after writing a leaf receipt.
            parent = path.parent
            while parent != root:
                parent = parent.parent
                sync_directory(parent)
    finally:
        temp.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def resources(attempt: Path) -> Iterator[None]:
    token = _CURRENT.set(attempt)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def plan_resource(args: list[str], image: str) -> Path | None:
    """Persist the exact name, launch command and binds BEFORE Docker creates it."""
    attempt = _CURRENT.get()
    if attempt is None:
        return None
    directory = attempt / "resources"
    directory.mkdir(mode=0o700, exist_ok=True)
    sync_directory(attempt)
    path = directory / (uuid.uuid4().hex + ".json")
    atomic_json(path, {"args": args, "image": image})
    return path


def observed_resource(path: Path | None, container_id: str) -> None:
    if path is not None:
        atomic_json(path.with_suffix(".observed"), {"container_id": container_id})


def docker_inspect(name: str) -> dict | None:
    """Only a successful exact-name list can establish absence; daemon errors pause."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    def run(args: list[str]) -> str:
        return subprocess.run(args, check=True, capture_output=True, env=env,
                              pass_fds=lock_fds()).stdout.decode()
    try:
        ids = run(["docker", "ps", "-aq", "--no-trunc", "--filter", "name=^/" + name + "$"]).split()
        if not ids:
            return None
        if len(ids) != 1:
            raise RecoveryPaused("resource_identity_uncertain")
        return json.loads(run(["docker", "inspect", ids[0]]))[0]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
        raise RecoveryPaused("resource_inspection_unavailable") from None


def resource_matches(receipt: dict, spec: dict, observed: dict | None) -> bool:
    args, image = receipt["args"], receipt["image"]
    name = args[args.index("--name") + 1]
    labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
    binds = set()
    for i, a in enumerate(args):
        if a == "--mount":
            fields = dict(part.split("=", 1) for part in args[i + 1].split(",") if "=" in part)
            binds.add((fields["type"], str(Path(fields["src"]).resolve()), fields["dst"],
                       "readonly" not in args[i + 1].split(",")))
    actual = {(m["Type"], m["Source"], m["Destination"], m["RW"]) for m in spec["Mounts"]
              if m["Type"] != "tmpfs"}
    return (spec["Name"] == "/" + name and spec["Image"] == image
            and (observed is None or observed["container_id"] == spec["Id"])
            and all(spec["Config"]["Labels"].get(k) == v for k, v in labels.items())
            and actual == binds and spec["Config"]["Cmd"] == args[args.index(image) + 1:]
            and spec["Config"]["Entrypoint"] == [args[args.index("--entrypoint") + 1]])


def inspect_resources(attempt: Path) -> list[dict]:
    rows = []
    for path in sorted((attempt / "resources").glob("*.json")):
        receipt = json.loads(path.read_text())
        args = receipt["args"]
        name = args[args.index("--name") + 1]
        spec = docker_inspect(name)
        observed_path = path.with_suffix(".observed")
        observed = json.loads(observed_path.read_text()) if observed_path.exists() else None
        if spec is not None and not resource_matches(receipt, spec, observed):
            raise RecoveryPaused("resource_identity_mismatch")
        rows.append({"name": name, "state": "absent" if spec is None else
                     "running" if spec["State"]["Running"] else "stopped",
                     "container_id": spec["Id"] if spec else None})
    return rows


def settle_resources(attempt: Path) -> list[dict]:
    """Inspect ALL first. Preserve live originals; remove only matched stopped ones."""
    rows = inspect_resources(attempt)
    if any(row["state"] == "running" for row in rows):
        raise RecoveryPaused("original_worker_still_running")
    for row in rows:
        if row["state"] == "stopped":
            # No force: if unexpectedly running, Docker refuses rather than killing it.
            subprocess.run(["docker", "rm", "--volumes", row["container_id"]],
                           check=True, capture_output=True, pass_fds=lock_fds(),
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            if docker_inspect(row["name"]) is not None:
                raise RecoveryPaused("resource_removal_unconfirmed")
    return rows


def restore_exports(attempt: Path, image: str) -> None:
    """Restore only known exports bound by original setup intent, after quiescence.

    Receipts are controller-owned. Never infer export ownership from a directory's
    existence alone. Repeating a partially completed restore is safe: only the
    sandbox UID/GID is changed, and chown never traverses symbolic links.
    """
    from scripts import lab_agent as agent

    if image != agent.IMAGE:
        raise RecoveryPaused("export_ownership_identity_uncertain")
    generations = [attempt, *(p.parent for p in session_directories(attempt)[2:])]
    allowed: dict[Path, tuple[str, ...]] = {
        attempt / "implementation": ("home", "work", "scratch", "output")}
    for generation in generations:
        allowed[generation / "review"] = ("home", "work", "scratch", "output")
        for role in ("incumbent", "candidate"):
            allowed[generation / ("evaluation-" + role)] = ("home", "work", "output")
    commands = {}
    # Validate every target before the first mutation, just as resource settlement
    # inspects every worker/helper before removing any stopped resource.
    for path in sorted((attempt / "resources").glob("*.json")):
        receipt = json.loads(path.read_text())
        args = receipt["args"]
        if "--entrypoint" not in args or args[args.index("--entrypoint") + 1] != "/bin/chown":
            continue
        if receipt["image"] != image or image not in args:
            raise RecoveryPaused("export_ownership_identity_uncertain")
        tail = args[args.index(image) + 1:]
        if "10000:10000" not in tail:
            continue  # A prior restoration is not a new ownership grant.
        mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
        roots = [root for root, children in allowed.items()
                 if mounts == [f"type=bind,src={root / child},dst=/{child}" for child in children]]
        if len(roots) != 1:
            raise RecoveryPaused("export_ownership_identity_uncertain")
        root = roots[0]
        children = allowed[root]
        destinations = ["/" + c for c in children]
        if (tail not in (["-R", "10000:10000", *destinations],
                         ["-R", "--no-dereference", "10000:10000", *destinations])
                or args[:3] != ["docker", "run", "--rm"]
                or args[args.index("--label") + 1] !=
                ("lk.lab=ticket-69" if len(children) == 4 else "lk.lab=ticket-66")):
            raise RecoveryPaused("export_ownership_identity_uncertain")
        for child in children:
            target = root / child
            if (not target.is_dir() or any(p.is_symlink() for p in
                    [target, *target.parents] if p == attempt or attempt in p.parents)):
                raise RecoveryPaused("export_ownership_identity_uncertain")
        commands[root] = args[:args.index(image)] + [image, "-R", "--no-dereference",
            "--from=10000:10000", f"{os.getuid()}:{os.getgid()}", *destinations]
    with resources(attempt):
        for command in commands.values():
            command[command.index("--name") + 1] = "lk-lab-recover-" + uuid.uuid4().hex
            plan_resource(command, image)
            try:
                try:
                    agent.run(command)
                finally:
                    agent.quiesce_helper(command, image)
            except Exception:
                raise RecoveryPaused("export_ownership_restore_failed") from None
    # Include newly persisted helper metadata in every subsequent settlement hash.
    if any(row["state"] != "absent" for row in inspect_resources(attempt)):
        raise RecoveryPaused("export_ownership_restore_unconfirmed")


def session_directories(attempt: Path) -> list[Path]:
    """Only controller-owned session roots, never worker scratch/output trees."""
    generations = sorted(p for p in attempt.glob("generation-*")
                         if p.is_dir() and p.name.removeprefix("generation-").isdigit())
    return [attempt / "implementation", attempt / "review", *(p / "review" for p in generations)]


def session_completion(attempt: Path) -> str:
    """Never replay a possibly dispatched inference or uncertain tool operation."""
    incomplete = False
    for session in session_directories(attempt):
        private = session / "private"
        for wire in private.glob("wire-*.json"):
            response = private / wire.name.replace("wire-", "response-", 1)
            if not response.exists():
                raise RecoveryPaused("model_completion_unknown")
        for request in private.glob("request-*.json"):
            wire = private / request.name.replace("request-", "wire-", 1)
            if not wire.exists():
                # The adapter durably saves wire in its HTTP request hook BEFORE
                # transport dispatch. A request alone never reached the provider.
                incomplete = True
        for intent in private.glob("tool-intent-*.json"):
            receipt = private / intent.name.replace("tool-intent-", "tool-")
            if not receipt.exists():
                # Once resources are conclusively quiescent this session cannot
                # continue, but it need not be replayed to finalize interrupted.
                incomplete = True
    return "incomplete" if incomplete else "settled"
