"""Trusted controller public seams; live Docker proof is an explicit pilot."""
from pathlib import Path

import pytest

from scripts.lab_runtime import provider_addresses, validate_pin, worker_command, prepare_attempt, verify_worker_spec


def test_worker_inspection_fails_closed_if_daemon_spec_differs(tmp_path: Path):
    spec: dict = {
        "Config": {"User": "10000:10000", "Labels": {"lk.lab": "ticket-65"}},
        "HostConfig": {"Privileged": False, "CapAdd": None, "CapDrop": ["ALL"],
                       "SecurityOpt": ["no-new-privileges:true"], "ReadonlyRootfs": True,
                       "NetworkMode": "container:helper-id", "Memory": 2147483648,
                       "MemorySwap": 2147483648, "NanoCpus": 2000000000, "PidsLimit": 256},
        "Mounts": [{"Type": "bind", "Source": str(tmp_path / child), "Destination": target, "RW": rw}
                   for child, target, rw in (("home", "/opt/data", True), ("work", "/work", True),
                                            ("output", "/output", True), ("corpus", "/corpus", False))],
    }
    verify_worker_spec(spec, tmp_path, tmp_path / "corpus", "helper-id")
    spec["HostConfig"]["Privileged"] = True
    with pytest.raises(ValueError):
        verify_worker_spec(spec, tmp_path, tmp_path / "corpus", "helper-id")
    spec["HostConfig"]["Privileged"] = False
    spec["Mounts"].append({"Type": "bind", "Source": "/", "Destination": "/host", "RW": False})
    with pytest.raises(ValueError):
        verify_worker_spec(spec, tmp_path, tmp_path / "corpus", "helper-id")



def test_prepare_attempt_is_fresh_and_never_overwrites_state(tmp_path: Path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "plugin.yaml").write_text("name: local_knowledge\n")
    (repo / "__init__.py").write_text("def register(ctx): pass\n")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Lab", "-c", "user.email=lab@example.invalid",
                    "commit", "-m", "fixture"], check=True, capture_output=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    attempt = tmp_path / "attempt"
    prepare_attempt(attempt, repo, revision)
    assert (attempt / "home/plugins/local_knowledge/plugin.yaml").is_file()
    assert not (attempt / "home/knowledge").exists()
    assert not (attempt / "home/memories").exists()
    assert not (attempt / "home/auth.json").exists()
    assert not (attempt / "home/cron").exists()
    config = (attempt / "home/config.yaml").read_text()
    assert '"auto_generate": false' in config
    assert '"mode": "off"' in config
    assert '"enabled": false' in config
    (attempt / "output/checkpoint").write_text("retained")
    with pytest.raises(FileExistsError):
        prepare_attempt(attempt, repo, revision)
    assert (attempt / "output/checkpoint").read_text() == "retained"
    second = tmp_path / "second"
    prepare_attempt(second, repo, revision)
    assert not (second / "output/checkpoint").exists()
    with pytest.raises(ValueError):
        prepare_attempt(tmp_path / "bad", repo, "HEAD")



def test_runtime_requires_an_immutable_image_identity():
    validate_pin("nousresearch/hermes-agent@sha256:" + "a" * 64)
    validate_pin("sha256:" + "b" * 64)
    for value in ("latest", "repo:main", "sha256:abc"):
        with pytest.raises(ValueError):
            validate_pin(value)


def test_provider_address_policy_rejects_private_and_ipv6():
    assert provider_addresses(["104.18.32.7"]) == ["104.18.32.7"]
    for address in ("127.0.0.1", "172.17.0.1", "192.168.1.213", "169.254.169.254", "100.64.0.1", "::1", "2606:4700::1"):
        with pytest.raises(ValueError):
            provider_addresses([address])
    with pytest.raises(ValueError):
        provider_addresses([])


def test_worker_contract_has_only_narrow_mounts_and_no_privilege(tmp_path: Path):
    args = worker_command("lk-lab-test-worker", "sha256:" + "b" * 64,
                          "lk-lab-test-net", tmp_path, tmp_path / "corpus",
                          {"chatgpt.com": ["104.18.32.7"], "auth.openai.com": ["104.18.33.7"]})
    assert args[args.index("--user") + 1] == "10000:10000"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--network") + 1] == "container:lk-lab-test-net"
    assert "no-new-privileges:true" in args
    assert "--read-only" in args
    assert "--memory" in args and "--cpus" in args and "--pids-limit" in args
    assert "--privileged" not in args and "--cap-add" not in args
    assert all("docker.sock" not in arg and "src=/home/alex/.hermes,dst=" not in arg for arg in args)
    assert sum(arg == "--mount" for arg in args) == 4
    assert any("dst=/corpus,readonly" in arg for arg in args)
    assert "--restart" in args and args[args.index("--restart") + 1] == "no"
    assert not any("budget" in arg or "timeout" in arg for arg in args)


def test_worker_rejects_unscoped_names_and_untrusted_endpoints(tmp_path: Path):
    with pytest.raises(ValueError):
        worker_command("production", "sha256:" + "b" * 64, "lk-lab-net", tmp_path, tmp_path, {})
    with pytest.raises(ValueError):
        worker_command("lk-lab-worker", "sha256:" + "b" * 64, "lk-lab-net", tmp_path, tmp_path,
                       {"private.example": ["8.8.8.8"]})
