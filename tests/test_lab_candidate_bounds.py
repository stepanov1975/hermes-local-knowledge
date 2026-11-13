"""Adversarial offline controls for untrusted candidate source boundaries."""
import hashlib
import json
import os
from pathlib import Path
import sys

from scripts import lab_workspace as source

import pytest

from scripts import lab_agent as agent
from scripts import lab_evaluator_runtime as evaluator
from scripts import lab_experiment as experiment


@pytest.mark.parametrize("reader", [agent.identity, evaluator.tree_hashes, experiment.workspace_files])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux sparse-file adversarial fixture")
def test_sparse_candidate_rejected_before_any_read(tmp_path, monkeypatch, reader):
    package = tmp_path / "hermes_local_knowledge"
    package.mkdir()
    path = package / "huge.py"
    with path.open("wb") as stream:
        stream.truncate(2**42)
    def forbidden(*args, **kwargs):
        pytest.fail("unbounded candidate read")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    with pytest.raises(ValueError, match="size"):
        reader(tmp_path)


def test_evaluator_work_mount_is_readonly(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "manifest.json").write_text("{}")
    command = evaluator.command("lk-lab-boundary-test", "sha256:" + "a" * 64,
                                tmp_path, corpus, tmp_path / "baseline", tmp_path / "probe", tmp_path / "tests")
    work = [value for value in command if "dst=/work," in value or value.endswith("dst=/work")]
    assert len(work) == 1 and work[0].endswith(",readonly")


@pytest.mark.parametrize("reader", [agent.identity, evaluator.tree_hashes, experiment.workspace_files])
def test_aggregate_rejected_before_open(tmp_path, monkeypatch, reader):
    package = tmp_path / "hermes_local_knowledge"
    package.mkdir()
    for name in ("a.py", "b.py"):
        (package / name).write_bytes(b"x" * 64)
    monkeypatch.setattr(source, "MAX_TREE_BYTES", 100)
    monkeypatch.setattr(source.os, "open", lambda *a, **kw: pytest.fail("opened before preflight"))
    with pytest.raises(ValueError, match="size"):
        reader(tmp_path)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux no-follow candidate descriptor reads")
def test_incremental_hashes_and_bounded_import(tmp_path, monkeypatch):
    package = tmp_path / "hermes_local_knowledge"
    package.mkdir()
    content = b"bounded source\n" * 10000
    (package / "a.py").write_bytes(content)
    monkeypatch.setattr(Path, "read_bytes", lambda *a: pytest.fail("whole-file hash read"))
    expected = {"hermes_local_knowledge/a.py": hashlib.sha256(content).hexdigest()}
    assert agent.identity(tmp_path)["files"] == expected
    assert evaluator.tree_hashes(tmp_path) == expected
    assert experiment.workspace_files(tmp_path) == {"hermes_local_knowledge/a.py": content}


def prepare_synthetic(attempt, repo, revision):
    for directory in ("home/plugins/local_knowledge", "work/plugin/tests", "output"):
        (attempt / directory).mkdir(parents=True, exist_ok=True)
    (attempt / "home/config.yaml").write_text('{"local_knowledge":{}}')
    (attempt / "work/plugin/source.py").write_text("original source")


@pytest.mark.parametrize("failure", ["verify", "setup", "remove", "restore"])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux no-follow private cleanup receipts")
def test_cleanup_restores_only_after_confirmed_quiescence(tmp_path, monkeypatch, failure):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "manifest.json").write_text("{}")
    attempt = tmp_path / "attempt"
    calls = []
    name, cid = "lk-lab-cleanup-test", "owned-id"
    def daemon(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": agent.IMAGE, "Config": {"Labels": {
                "org.opencontainers.image.revision": agent.SOURCE}}}])
        if args[:2] == ["docker", "create"]:
            return cid
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([{"Id": cid, "Name": "/" + name + "-worker",
                                "Config": {"Labels": {"lk.lab": "ticket-66"}}}])
        if args[:2] == ["docker", "rm"] and failure == "remove":
            raise ValueError("remove failed")
        if args[:2] == ["docker", "run"]:
            helper = args[args.index("--name") + 1]
            if (failure == "setup" and helper.endswith("-setup")) or (failure == "restore" and helper.endswith("-restore")):
                raise ValueError("helper failed")
        return ""
    monkeypatch.setattr(evaluator, "run", daemon)
    monkeypatch.setattr(evaluator, "prepare_attempt", prepare_synthetic)
    monkeypatch.setattr(evaluator, "corpus_integrity", lambda p: {"settings": {}})
    monkeypatch.setattr(evaluator, "verify_spec", lambda *args: (_ for _ in ()).throw(ValueError("verify failed")))
    with pytest.raises(ValueError):
        evaluator.evaluate_revision(attempt=attempt, repo=tmp_path, revision="a" * 40,
            corpus=corpus, image=agent.IMAGE, name=name,
            benchmark={"corpus_manifest_sha256": evaluator.digest(corpus / "manifest.json")})
    receipt = json.loads((attempt / "cleanup.json").read_text())
    restore = [i for i, args in enumerate(calls) if name + "-restore" in args]
    if failure == "remove":
        assert not restore
        assert receipt["worker_removed"] is False
    else:
        assert restore
        if failure != "setup":
            removed = next(i for i, args in enumerate(calls) if args[:2] == ["docker", "rm"])
            confirmed = next(i for i, args in enumerate(calls) if args[:3] == ["docker", "ps", "--all"])
            assert removed < confirmed < restore[0]
        assert receipt["ownership_restored"] is (failure != "restore")
        assert receipt["worker_removed"] is True
        assert receipt["cleanup_complete"] is (failure != "restore")


@pytest.mark.skipif(sys.platform != "linux" or os.environ.get("LK_LAB_SYNTHETIC_DOCKER") != "1",
                    reason="Explicit opt-in offline Docker boundary proof")
def test_real_offline_evaluator_failure_restores_owner(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir(mode=0o755)
    (corpus / "mapping.json").write_text("[]")
    mapping = corpus / "mapping.json"
    (corpus / "manifest.json").write_text(json.dumps({"settings": {}, "inputs": {
        "mapping.json": {"sha256": evaluator.digest(mapping), "size": mapping.stat().st_size}}}))
    probe = tmp_path / "synthetic-probe.py"
    probe.write_text("""import errno, json, pathlib, sys
proof = {}
for source in ('/work/plugin/source.py', '/opt/data/plugins/local_knowledge/transient.py'):
    try:
        pathlib.Path(source).write_text('transient mutation')
    except OSError as exc:
        assert exc.errno == errno.EROFS, repr(exc)
        proof[source] = 'EROFS'
    else:
        raise RuntimeError('candidate mount was writable')
pathlib.Path('/output/write-proof.json').write_text(json.dumps(proof))
print('LK68 '+json.dumps({'ready': True, 'mapping_count': 0}), flush=True)
for line in sys.stdin:
    raise RuntimeError('no benchmark queries permitted')
""")
    original_command, original_verify = evaluator.command, evaluator.verify_spec
    def command(name, image, attempt, corpus, baseline, ignored, tests):
        return original_command(name, image, attempt, corpus, baseline, probe, tests)
    def verify(spec, attempt, corpus, baseline, ignored, tests):
        return original_verify(spec, attempt, corpus, baseline, probe, tests)
    original_bounded = evaluator.bounded_process
    def fail_tests(args, **kwargs):
        # Exercise a real failing exec, but no plugin import, provider or scoring.
        args = args[:args.index('/opt/lab-tools/bin/python')] + [
            '/opt/lab-tools/bin/python', '-c', 'raise SystemExit(7)']
        return original_bounded(args, **kwargs)
    monkeypatch.setattr(evaluator, "prepare_attempt", prepare_synthetic)
    monkeypatch.setattr(evaluator, "command", command)
    monkeypatch.setattr(evaluator, "verify_spec", verify)
    monkeypatch.setattr(evaluator, "bounded_process", fail_tests)
    attempt = tmp_path / "attempt"
    with pytest.raises(ValueError, match="Trusted candidate tests failed"):
        evaluator.evaluate_revision(attempt=attempt, repo=tmp_path, revision="a" * 40,
            corpus=corpus, image=agent.IMAGE, name="lk-lab-synthetic-boundary-" + str(os.getpid()),
            benchmark={"corpus_manifest_sha256": evaluator.digest(corpus / "manifest.json")})
    receipt = json.loads((attempt / "cleanup.json").read_text())
    assert receipt == {"worker_removed": True, "cleanup_complete": True, "ownership_restored": True,
                       "setup_quiescent": True, "restore_quiescent": True}
    assert json.loads((attempt / "output/write-proof.json").read_text()) == {
        "/work/plugin/source.py": "EROFS", "/opt/data/plugins/local_knowledge/transient.py": "EROFS"}
    assert not (attempt / "home/plugins/local_knowledge/transient.py").exists()
    for child in ("home", "work", "output"):
        assert (attempt / child).stat().st_uid == os.getuid()
    assert (attempt / "work/plugin/source.py").read_text() == "original source"
    assert not (attempt / "aggregate.json").exists()


@pytest.mark.parametrize("fault", ["none", "inspect", "identity", "mount", "remove", "readback", "restore_timeout"])
def test_accepted_setup_timeout_quiesces_before_restore(tmp_path, monkeypatch, fault):
    import subprocess
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "manifest.json").write_text("{}")
    attempt = tmp_path / "attempt"
    live, calls = {}, []
    restored = []

    def daemon(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": agent.IMAGE, "Config": {"Labels": {
                "org.opencontainers.image.revision": agent.SOURCE}}}])
        if args[:2] == ["docker", "run"]:
            name = args[args.index("--name") + 1]
            if name.endswith("-restore"):
                assert not live, "setup must be conclusively absent before restore"
                restored.append(name)
                if fault != "restore_timeout":
                    return ""
            mounts = []
            for i, arg in enumerate(args):
                if arg == "--mount":
                    fields = dict(part.split("=", 1) for part in args[i + 1].split(","))
                    mounts.append({"Type": fields["type"], "Source": fields["src"],
                                   "Destination": fields["dst"], "RW": True})
            live[name] = {"Id": name + "-id", "Name": "/" + name, "Image": agent.IMAGE,
                "Config": {"Labels": {"lk.lab": "ticket-66"}, "Entrypoint": ["/bin/chown"],
                           "Cmd": args[args.index(agent.IMAGE) + 1:]},
                "Mounts": mounts, "State": {"Running": True}}
            raise subprocess.TimeoutExpired(args, 120)
        if args[:2] == ["docker", "ps"]:
            return "\n".join(s["Id"] for n, s in live.items() if "name=^/" + n + "$" in args)
        if args[:2] == ["docker", "inspect"]:
            if fault == "inspect":
                raise subprocess.CalledProcessError(1, args)
            spec = next(s for s in live.values() if s["Id"] == args[-1])
            if fault == "identity":
                spec["Config"]["Labels"]["lk.lab"] = "unowned"
            if fault == "mount":
                spec["Mounts"][0]["Source"] = "/unowned"
            return json.dumps([spec])
        if args[:2] == ["docker", "rm"]:
            if fault == "remove":
                raise subprocess.CalledProcessError(1, args)
            if fault != "readback":
                live.pop(next(n for n, s in live.items() if s["Id"] == args[-1]))
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(evaluator, "run", daemon)
    monkeypatch.setattr(evaluator, "prepare_attempt", prepare_synthetic)
    monkeypatch.setattr(evaluator, "corpus_integrity", lambda p: {"settings": {}})
    with pytest.raises((subprocess.SubprocessError, ValueError)):
        evaluator.evaluate_revision(attempt=attempt, repo=tmp_path, revision="a" * 40,
            corpus=corpus, image=agent.IMAGE, name="lk-lab-accepted-timeout",
            benchmark={"corpus_manifest_sha256": evaluator.digest(corpus / "manifest.json")})
    receipt = json.loads((attempt / "cleanup.json").read_text())
    assert receipt["cleanup_complete"] is (fault == "none")
    assert receipt["ownership_restored"] is (fault == "none")
    assert bool(restored) is (fault in {"none", "restore_timeout"})
    if fault in {"identity", "mount", "inspect"}:
        assert not any(args[:2] == ["docker", "rm"] for args in calls)
    if fault in {"none", "restore_timeout"}:
        assert not live
        assert receipt["setup_quiescent"] and receipt["restore_quiescent"]


@pytest.mark.skipif(os.environ.get("LK_LAB_SYNTHETIC_DOCKER") != "1",
                    reason="Explicit opt-in offline Docker helper quiescence proof")
def test_real_offline_owned_helper_is_quiescent_before_return():
    name = "lk-lab-synthetic-helper-" + str(os.getpid())
    args = ["docker", "run", "--detach", "--rm", "--name", name, "--label", "lk.lab=ticket-66",
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--user", "10000:10000",
            "--security-opt", "no-new-privileges:true", "--memory", "64m", "--pids-limit", "16",
            "--tmpfs", "/opt/data:rw,nosuid,nodev,size=1048576",
            "--entrypoint", "/bin/sleep", agent.IMAGE, "300"]
    image_id = json.loads(evaluator.run(["docker", "image", "inspect", agent.IMAGE]))[0]["Id"]
    try:
        cid = evaluator.run(args).strip()
        before = json.loads(evaluator.run(["docker", "inspect", cid]))[0]
        assert before["State"]["Running"] is True
        evaluator.quiesce_helper(args, image_id)
        assert not evaluator.run(["docker", "ps", "-aq", "--no-trunc", "--filter", "name=^/" + name + "$"]).strip()
    finally:
        evaluator.quiesce_helper(args, image_id)
