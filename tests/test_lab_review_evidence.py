"""Offline receipt/projection regressions; synthetic checks are never production evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts import lab_agent as agent, lab_experiment as lab, lab_review_evidence as proof
from tests.test_lab_agent import review_inputs, setup_session, spec_for


@pytest.fixture
def packet(tmp_path, monkeypatch):
    kwargs, _ = setup_session(tmp_path, monkeypatch)
    review_inputs(kwargs, tmp_path, monkeypatch)
    return kwargs, lab.read_json(kwargs["evidence"]), tmp_path / "review-proof"


def test_explicit_projection_and_receipts_exclude_controller_bytes(packet):
    kwargs, value, directory = packet
    assert value["projection"]["omitted"]["scripts/controller.py"]["reason"]
    assert "scripts/controller.py" not in value["projection"]["admitted"]
    assert "excluded synthetic controller" not in json.dumps(value)
    assert {p.name for p in directory.iterdir()} == proof.PROOF_FILES
    assert "full suite" in value["runnable_scope"]["scope"]
    assert "incomplete previews" in value["trusted_full_checks"]
    workspace = agent.export(kwargs["attempt"], kwargs["repo"], kwargs["revision"])
    proof.prepare(kwargs["evidence"], kwargs["attempt"], workspace,
                  kwargs["repo"], kwargs["revision"], agent.IMAGE)
    assert proof.inventory(kwargs["attempt"] / "inputs/review-proof") == value["proof_files"]
    assert all(not rw for _, target, rw in agent.mounts(kwargs["attempt"], kwargs["corpus"], "review", True)
               if target.startswith("/inputs/"))
    assert not any("review-proof" in target for _, target, _ in
                   agent.mounts(kwargs["attempt"], kwargs["corpus"], "implementation", False))


@pytest.mark.parametrize("phase", ["build", "prepare"])
def test_proof_directory_readable_by_worker_under_private_umask(packet, tmp_path, phase):
    kwargs, value, _ = packet
    private = tmp_path / "private-context"
    private.mkdir(mode=0o700)
    previous = os.umask(0o077)
    try:
        if phase == "build":
            directory = private / "review-proof"
            result = proof.build(repo=kwargs["repo"], revision=kwargs["revision"],
                binding=value["binding"], report=value["draft_report"],
                checks_root=tmp_path / "review-checks", proof=directory)
        else:
            workspace = agent.export(kwargs["attempt"], kwargs["repo"], kwargs["revision"])
            result = proof.prepare(kwargs["evidence"], kwargs["attempt"], workspace,
                kwargs["repo"], kwargs["revision"], agent.IMAGE)
            directory = kwargs["attempt"] / "inputs/review-proof"
            private = kwargs["attempt"]
    finally:
        os.umask(previous)
    assert directory.stat().st_mode & 0o777 == 0o755
    assert private.stat().st_mode & 0o077 == 0
    assert proof.inventory(directory) == result["proof_files"]
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in directory.iterdir())


@pytest.mark.parametrize("mutation", ["bytes", "mode", "extra", "symlink"])
def test_export_drift_rejected(packet, mutation):
    kwargs, value, _ = packet
    workspace = agent.export(kwargs["attempt"], kwargs["repo"], kwargs["revision"])
    path = workspace / "README.md"
    if mutation == "bytes":
        path.write_text("changed")
    elif mutation == "mode":
        path.chmod(0o755)
    elif mutation == "extra":
        (workspace / "unexpected.md").write_text("unexpected")
    else:
        path.unlink()
        path.symlink_to(kwargs["development"])
    with pytest.raises(ValueError):
        proof.validate_projection(workspace, value["projection"])


@pytest.mark.parametrize("field", ["revision", "tree", "image", "controller", "checks_sha256"])
def test_identity_mismatch_rejected(packet, field):
    kwargs, value, directory = packet
    value["binding"][field] = "incorrect"
    with pytest.raises(ValueError):
        proof.validate(value, directory, revision=kwargs["revision"], image=agent.IMAGE)


@pytest.mark.parametrize("name", sorted(proof.PROOF_FILES))
def test_every_receipt_hash_and_size_checked(packet, name):
    kwargs, value, directory = packet
    path = directory / name
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="hash/size"):
        proof.validate(value, directory, revision=kwargs["revision"], image=agent.IMAGE)


@pytest.mark.parametrize("kind", ["symlink", "extra", "missing"])
def test_proof_narrow_regular_inventory(packet, kind):
    kwargs, value, directory = packet
    if kind == "extra":
        (directory / "private-cases.jsonl").write_text("SYNTHETIC_HIDDEN_LABEL")
    else:
        path = directory / "full_pytest.stdout"
        path.unlink()
        if kind == "symlink":
            path.symlink_to(kwargs["development"])
    with pytest.raises(ValueError):
        proof.validate(value, directory, revision=kwargs["revision"], image=agent.IMAGE)


def test_old_unexplained_packet_and_implementation_proof_refused(packet):
    kwargs, value, directory = packet
    with pytest.raises((ValueError, KeyError)):
        proof.validate({"binding": value["binding"], "draft_report": {}}, directory,
                       revision=kwargs["revision"], image=agent.IMAGE)
    with pytest.raises(ValueError, match="review-only"):
        agent.run_session(**kwargs)
    kwargs["role"] = "review"
    kwargs.pop("evidence")
    with pytest.raises(ValueError, match="requires explicit"):
        agent.run_session(**kwargs)


def test_rehashed_manifest_forgery_rejected_before_dispatch(packet):
    kwargs, value, _ = packet
    value["projection"]["policy"] = "invented policy"
    value["projection_sha256"] = lab.sha(value["projection"])
    kwargs["evidence"].write_text(json.dumps(value))
    workspace = agent.export(kwargs["attempt"], kwargs["repo"], kwargs["revision"])
    with pytest.raises(ValueError, match="provenance"):
        proof.prepare(kwargs["evidence"], kwargs["attempt"], workspace,
                      kwargs["repo"], kwargs["revision"], agent.IMAGE)


def test_rehashed_contradictory_cleanup_rejected(packet):
    kwargs, value, directory = packet
    path = directory / "full_pytest.cleanup.json"
    data = json.loads(path.read_text())
    data["cleanup_complete"] = False
    path.chmod(0o644)
    path.write_text(json.dumps(data))
    value["proof_files"] = proof.inventory(directory)
    with pytest.raises(ValueError, match="receipt binding"):
        proof.validate(value, directory, revision=kwargs["revision"], image=agent.IMAGE)


@pytest.mark.parametrize("kind", ["mode", "proof", "packet"])
def test_post_dispatch_drift_cannot_complete(packet, monkeypatch, kind):
    kwargs, _, _ = packet
    kwargs["role"] = "review"
    monkeypatch.setattr(agent, "inspect", lambda name: spec_for(name, agent.mounts(
            kwargs["attempt"], kwargs["corpus"], "review", True)))
    def send(*args):
        if kind == "mode":
            (kwargs["attempt"] / "work/plugin/README.md").chmod(0o755)
        elif kind == "proof":
            path = kwargs["attempt"] / "inputs/review-proof/full_pytest.stdout"
            path.chmod(0o644)
            path.write_text("changed")
        else:
            path = kwargs["attempt"] / "inputs/evidence.json"
            path.chmod(0o644)
            path.write_text("{}")
        return {"message": {"role": "assistant", "content": json.dumps({
            "approved": True, "shortcuts_checked": True, "compatibility_checked": True,
            "report_checked": True, "findings": []})}, "usage": {}}
    monkeypatch.setattr(agent.model, "send", send)
    with pytest.raises(ValueError):
        agent.run_session(**kwargs)
    assert not (kwargs["attempt"] / "private/result.json").exists()
    assert not lab.read_json(kwargs["attempt"] / "private/status.json")["complete"]


@pytest.mark.parametrize("kind", ["cleanup", "exit", "size", "worker"])
def test_semantic_receipt_contradictions_fail_even_with_fresh_hashes(packet, kind):
    kwargs, value, directory = packet
    name = "full_pytest.cleanup.json" if kind == "cleanup" else "full_pytest.output.json"
    path = directory / name
    data = lab.read_json(path)
    if kind == "cleanup":
        data["cleanup_complete"] = False
    elif kind == "exit":
        data["exit_code"] = 3
    elif kind == "size":
        data["stored_bytes"]["stdout"] = 999
    else:
        data["args"][-1] = "wrong-worker"
    path.chmod(0o644)
    path.write_text(json.dumps(data))
    checks_path = directory / "checks.json"
    checks = lab.read_json(checks_path)
    checks["checks"][0]["evidence_files"] = proof.receipt_files(directory, "full_pytest")
    checks_path.chmod(0o644)
    checks_path.write_text(json.dumps(checks))
    value["binding"]["checks_sha256"] = lab.sha(checks)
    value["proof_files"] = proof.inventory(directory)
    with pytest.raises(ValueError):
        proof.validate(value, directory, revision=kwargs["revision"], image=agent.IMAGE)


def test_builder_does_not_copy_sibling_hidden_data(packet, tmp_path):
    kwargs, value, _ = packet
    (tmp_path / "review-checks/private-cases.jsonl").write_text("SYNTHETIC_HIDDEN_LABEL")
    second = proof.build(repo=kwargs["repo"], revision=kwargs["revision"], binding=value["binding"],
        report={}, checks_root=tmp_path / "review-checks", proof=tmp_path / "second-proof")
    assert "SYNTHETIC_HIDDEN_LABEL" not in json.dumps(second)
    assert {p.name for p in (tmp_path / "second-proof").iterdir()} == proof.PROOF_FILES
    assert all("SYNTHETIC_HIDDEN_LABEL" not in p.read_text() for p in (tmp_path / "second-proof").iterdir())


def test_check_failure_format_does_not_export_source_or_captured_content(tmp_path):
    path = tmp_path / "test_synthetic.py"
    path.write_text("def test_failure():\n    print('SYNTHETIC_CAPTURE_PRIVATE')\n"
                    "    assert False, 'SYNTHETIC_SOURCE_PRIVATE'\n")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no", "--show-capture=no",
                             "--disable-warnings", "--no-summary", "-p", "no:cacheprovider", str(path)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert "SYNTHETIC_CAPTURE_PRIVATE" not in result.stdout + result.stderr
    assert "SYNTHETIC_SOURCE_PRIVATE" not in result.stdout + result.stderr


def test_declared_scope_runs_against_actual_filtered_git_export(tmp_path):
    """Use actual source bytes, including in the immutable non-Git check export."""
    source = Path(__file__).resolve().parents[1]
    repo = tmp_path / "source-snapshot"
    shutil.copytree(source, repo, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"))
    lab.git(repo, "init", "-q")
    lab.git(repo, "add", ".")
    lab.git(repo, "commit", "-m", "disposable actual source snapshot")
    revision = lab.git(repo, "rev-parse", "HEAD").decode().strip()
    workspace = agent.export(tmp_path / "real-filtered-export", repo, revision)
    manifest = proof.projection(repo, revision)
    proof.validate_projection(workspace, manifest)
    assert not (workspace / "scripts").exists()
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", proof.SCOPE],
                            cwd=workspace, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[100%]" in result.stdout
