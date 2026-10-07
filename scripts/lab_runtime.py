"""Small stdlib-only trusted Docker controller primitives (never plugin-owned)."""
from __future__ import annotations

import ipaddress
import io
import json
import shutil
import tarfile
import os
import re
import subprocess
from pathlib import Path


def scoped_name(name: str) -> str:
    if not re.fullmatch(r"lk-lab-[a-z0-9-]{1,60}", name):
        raise ValueError("Name must belong to lk-lab-* scope")
    return name


def worker_command(name: str, image: str, helper: str, attempt: Path,
                   corpus: Path, hosts: dict[str, list[str]]) -> list[str]:
    scoped_name(name)
    scoped_name(helper)
    validate_pin(image)
    if set(hosts) != set(PROVIDERS):
        raise ValueError("Only the fixed model provider endpoints are supported")
    args = ["docker", "create", "--name", name, "--label", LABEL,
            "--user", "10000:10000", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--read-only",
            "--memory", "2g", "--memory-swap", "2g", "--cpus", "2",
            "--pids-limit", "256", "--restart", "no", "--init",
            "--network", "container:" + helper, "--workdir", "/work/plugin",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
            "--env", "HOME=/opt/data", "--env", "HERMES_HOME=/opt/data",
            "--env", "TMPDIR=/tmp", "--env", "CODEX_HOME=/opt/data/no-codex",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "HERMES_WRITE_SAFE_ROOT=/work"]
    # /opt/data overlays the official image VOLUME: no anonymous live-state volume.
    for source, target, readonly in ((attempt / "home", "/opt/data", False),
                                     (attempt / "work", "/work", False),
                                     (attempt / "output", "/output", False),
                                     (corpus, "/corpus", True)):
        path = source.resolve()
        if "," in str(path):
            raise ValueError("Docker mount path contains a separator")
        args += ["--mount", f"type=bind,src={path},dst={target}" + (",readonly" if readonly else "")]
    # Docker forbids --add-host with container: networking; helper owns /etc/hosts.
    args += ["--entrypoint", PYTHON, image, "/opt/lab/native_probe.py"]
    return args

PROVIDERS = ("chatgpt.com", "auth.openai.com")
LABEL = "lk.lab=ticket-65"
PYTHON = "/opt/hermes/.venv/bin/python"


def validate_pin(image: str) -> str:
    if not re.fullmatch(r"(?:[a-zA-Z0-9./_-]+@)?sha256:[0-9a-f]{64}", image):
        raise ValueError("An exact image ID or repository digest is required")
    return image


def provider_addresses(addresses: list[str]) -> list[str]:
    if not addresses:
        raise ValueError("Empty provider address set")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.version != 4 or not ip.is_global or ip.is_multicast:
            raise ValueError("Only public unicast provider IPv4 addresses are allowed")
    return sorted(set(addresses))


def verify_worker_spec(spec: dict, attempt: Path, corpus: Path, helper_id: str) -> None:
    """Enforce daemon readback, not merely a successful create command."""
    config, host = spec["Config"], spec["HostConfig"]
    expected = {"Privileged": False, "CapDrop": ["ALL"], "ReadonlyRootfs": True,
                "NetworkMode": "container:" + helper_id, "Memory": 2147483648,
                "MemorySwap": 2147483648, "NanoCpus": 2000000000, "PidsLimit": 256}
    if (config["User"] != "10000:10000" or config["Labels"].get("lk.lab") != "ticket-65"
            or host.get("CapAdd") or "no-new-privileges:true" not in host["SecurityOpt"]
            or any(host.get(key) != value for key, value in expected.items())):
        raise ValueError("Docker worker isolation/resource readback differs from contract")
    mounts = {(item["Type"], item["Source"], item["Destination"], item["RW"]) for item in spec["Mounts"]}
    wanted = {("bind", str((attempt / child).resolve()), target, rw)
              for child, target, rw in (("home", "/opt/data", True), ("work", "/work", True),
                                       ("output", "/output", True))}
    wanted.add(("bind", str(corpus.resolve()), "/corpus", False))
    # Docker includes explicitly requested tmpfs in Mounts on some daemon versions.
    mounts.discard(("tmpfs", "", "/tmp", True))
    if mounts != wanted:
        raise ValueError("Docker worker mounts differ from the four narrow approved binds")


def prepare_attempt(attempt: Path, repo: Path, revision: str) -> None:
    """Export only an exact Git object; never copy checkout state or credentials."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("An exact plugin commit is required, not a branch or HEAD")
    resolved = run(["git", "-C", str(repo), "rev-parse", revision + "^{commit}"]).strip()
    if resolved != revision:
        raise ValueError("Plugin commit identity mismatch")
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", revision],
                             env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                             check=True, capture_output=True, timeout=120).stdout
    attempt.mkdir(mode=0o700)  # Refuse reuse even after a failed attempt.
    work = attempt / "work" / "plugin"
    work.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("Unsafe plugin archive member")
            if any(part.startswith(".env") or part in {"auth.json", "id_rsa", "id_ed25519"} for part in path.parts):
                raise ValueError("Credential-bearing archive path")
            destination = work / path
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                assert source is not None
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(member.mode & 0o777)
    home = attempt / "home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(work, home / "plugins/local_knowledge")
    (home / "scanner").mkdir()
    # JSON is a strict YAML subset; do not copy any production config or jobs.
    config = {"plugins": {"enabled": ["local_knowledge"]},
              "security": {"allow_lazy_installs": False},
              "terminal": {"backend": "local"},
              "local_knowledge": {
                  "source_root": "/corpus", "hermes_home": "/opt/data/scanner",
                  "state_dir": "/opt/data/knowledge", "index_max_age_seconds": 0,
                  "okf": {"enabled": False, "auto_generate": False},
                  "implicit_feedback": {"enabled": False},
                  "verified_routing": {"mode": "off"}}}
    (home / "config.yaml").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (attempt / "output").mkdir()
    (attempt / "identity.json").write_text(json.dumps({"plugin_commit": revision}) + "\n")


def run(args: list[str], *, timeout: int | None = 120) -> str:
    # No inherited provider credentials, import paths, Docker contexts or proxies.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, output=result.stdout, stderr=result.stderr)
    return result.stdout
