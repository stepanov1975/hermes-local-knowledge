#!/usr/bin/env python3
"""Operator-invoked trusted controller for the bounded ticket-65 native pilot.

No scheduler, model authentication, worker-owned Docker or campaign scoring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time

from scripts.lab_runtime import LABEL, PROVIDERS, prepare_attempt, provider_addresses, run, scoped_name, validate_pin, verify_worker_spec, worker_command


def validate_corpus(corpus: Path) -> list[dict[str, object]]:
    """Only a small, explicitly reviewed manifest, never recursive corpus discovery."""
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= 16:
        raise ValueError("Pilot requires 1..16 approved Markdown artifacts")
    names = set()
    for item in manifest:
        name = item["path"]
        if Path(name).name != name or not name.endswith(".md") or name in names:
            raise ValueError("Only distinct flat Markdown artifacts are accepted")
        names.add(name)
        path = corpus / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            raise ValueError("Unsafe or oversized corpus artifact")
        if not item.get("review") or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Unreviewed or changed corpus artifact")
    if {p.name for p in corpus.iterdir()} != names | {"manifest.json"}:
        raise ValueError("Unapproved corpus contents")
    return manifest


def inspect(name: str) -> dict:
    return json.loads(run(["docker", "inspect", name]))[0]


def remove_owned_container(name: str) -> None:
    scoped_name(name)
    obj = inspect(name)
    if obj["Config"]["Labels"].get("lk.lab") != "ticket-65":
        raise ValueError("Refusing cleanup of an unowned container")
    run(["docker", "rm", "-f", "-v", name])
    try:
        inspect(name)
    except subprocess.CalledProcessError:
        return
    raise RuntimeError("Container remained after cleanup")


def pilot(*, attempt: Path, repo: Path, revision: str, corpus: Path, image: str,
          network_image: str, name: str, query: str, blocked: list[str]) -> None:
    """A fresh launch, with indefinite worker wait and exact scoped cleanup."""
    scoped_name(name)
    validate_pin(image)
    validate_pin(network_image)
    manifest = validate_corpus(corpus)
    hosts = {host: provider_addresses([str(info[4][0]) for info in
             socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)]) for host in PROVIDERS}
    # Evidence directory is outside all worker mounts.
    prepare_attempt(attempt, repo, revision)
    (attempt / "approved-corpus.json").write_text(json.dumps(manifest, indent=2) + "\n")
    runtime_identity = json.loads(run(["docker", "image", "inspect", image]))[0]
    helper_identity = json.loads(run(["docker", "image", "inspect", network_image]))[0]
    (attempt / "images.json").write_text(json.dumps({"runtime": runtime_identity,
                                                    "network_helper": helper_identity}, indent=2) + "\n")
    (attempt / "provider-addresses.json").write_text(json.dumps(hosts, indent=2) + "\n")
    # A trusted one-shot setup helper may chown only these newly exported directories.
    setup = name + "-setup"
    setup_args = ["docker", "run", "--rm", "--name", setup, "--label", LABEL,
                  "--network", "none", "--user", "0:0", "--cap-drop", "ALL",
                  "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
                  "--security-opt", "no-new-privileges:true", "--read-only",
                  "--memory", "64m", "--cpus", "0.25", "--pids-limit", "16",
                  "--entrypoint", "/bin/chown"]
    for child in ("home", "work", "output"):
        setup_args += ["--mount", f"type=bind,src={attempt / child},dst=/{child}"]
    run(setup_args + [image, "-R", "10000:10000", "/home", "/work", "/output"])
    network = name + "-bridge"
    helper = name + "-net"
    worker = name + "-worker"
    created: list[str] = []
    network_created = False
    cleanup: list[str] = []
    try:
        run(["docker", "network", "create", "--label", LABEL, network])
        network_created = True
        network_config = json.loads(run(["docker", "network", "inspect", network]))[0]
        gateway = network_config["IPAM"]["Config"][0]["Gateway"]
        (attempt / "network.json").write_text(json.dumps(network_config, indent=2) + "\n")
        helper_args = ["docker", "create", "--name", helper, "--label", LABEL,
                       "--network", network, "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                       "--security-opt", "no-new-privileges:true", "--read-only",
                       "--tmpfs", "/run:rw,nosuid,nodev,size=1048576",
                       "--memory", "64m", "--memory-swap", "64m", "--cpus", "0.25",
                       "--pids-limit", "32", "--restart", "no"]
        for host, addresses in hosts.items():
            for address in addresses:
                helper_args += ["--add-host", host + ":" + address]
        helper_args += [network_image, *sorted({ip for ips in hosts.values() for ip in ips})]
        run(helper_args)
        created.append(helper)
        run(["docker", "start", helper])
        # Bounded infrastructure readiness, never a worker lifetime allowance.
        deadline = time.monotonic() + 30
        while True:
            try:
                run(["docker", "exec", helper, "test", "-f", "/run/ready"])
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline or not inspect(helper)["State"]["Running"]:
                    raise RuntimeError("Network helper failed closed: " + run(["docker", "logs", helper]))
                time.sleep(0.1)
        (attempt / "firewall-before.txt").write_text(run(["docker", "exec", helper, "iptables-save", "-c"]))
        (attempt / "firewall-ipv6.txt").write_text(run(["docker", "exec", helper, "ip6tables-save", "-c"]))
        args = worker_command(worker, image, helper, attempt, corpus, hosts)
        args += ["--query", query]
        for target in [*blocked, f"{gateway}:22", "127.0.0.11:53", "169.254.169.254:80",
                       "1.1.1.1:443", "[2606:4700:4700::1111]:443"]:
            args += ["--blocked", target]
        (attempt / "worker-command.json").write_text(json.dumps(args, indent=2) + "\n")
        run(args)
        created.append(worker)
        worker_spec = inspect(worker)
        verify_worker_spec(worker_spec, attempt, corpus, inspect(helper)["Id"])
        (attempt / "worker-before.json").write_text(json.dumps(worker_spec, indent=2) + "\n")
        (attempt / "helper.json").write_text(json.dumps(inspect(helper), indent=2) + "\n")
        run(["docker", "start", worker])
        # No elapsed-time cutoff. Only explicit operator cancellation stops a worker.
        exit_code = run(["docker", "wait", worker], timeout=None).strip()
        (attempt / "worker-after.json").write_text(json.dumps(inspect(worker), indent=2) + "\n")
        (attempt / "worker.log").write_text(run(["docker", "logs", worker]))
        (attempt / "firewall-after.txt").write_text(run(["docker", "exec", helper, "iptables-save", "-c"]))
        if exit_code != "0":
            raise RuntimeError(f"Worker exited {exit_code}; private evidence: {attempt}")
        for filename in ("native-search.json", "native-feedback.json", "pilot.json", "checkpoint.json"):
            if not (attempt / "output" / filename).is_file():
                raise RuntimeError("Missing worker output: " + filename)
    finally:
        for container in reversed(created):
            remove_owned_container(container)
            cleanup.append(container)
        if network_created:
            obj = json.loads(run(["docker", "network", "inspect", network]))[0]
            if obj["Labels"].get("lk.lab") != "ticket-65":
                raise ValueError("Refusing unowned network cleanup")
            run(["docker", "network", "rm", network])
            try:
                run(["docker", "network", "inspect", network])
            except subprocess.CalledProcessError:
                cleanup.append(network)
            else:
                raise RuntimeError("Network remained after cleanup")
        retained = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (attempt / "output").iterdir() if p.is_file()}
        (attempt / "cleanup.json").write_text(json.dumps({"removed": cleanup,
                  "retained_output_sha256": retained}, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--plugin-repo", type=Path, required=True)
    parser.add_argument("--plugin-commit", required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--image", required=True, help="Immutable lab image ID/digest")
    parser.add_argument("--network-image", required=True, help="Immutable trusted helper image ID/digest")
    parser.add_argument("--name", required=True, help="Unique lk-lab-* prefix")
    parser.add_argument("--query", required=True)
    parser.add_argument("--blocked", action="append", required=True, help="Known homelab IPv4:port; no HTTP/auth")
    args = parser.parse_args()
    # Paths are explicit; never autodiscover/copy production state.
    pilot(attempt=args.attempt.resolve(), repo=args.plugin_repo.resolve(), revision=args.plugin_commit,
          corpus=args.corpus.resolve(), image=args.image, network_image=args.network_image,
          name=args.name, query=args.query, blocked=args.blocked)
    print("Pilot complete; private evidence: " + str(args.attempt))


if __name__ == "__main__":
    main()
