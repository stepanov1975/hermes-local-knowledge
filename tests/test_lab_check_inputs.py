"""Public trusted-check inputs stay separate from exact candidate source."""
from __future__ import annotations

import hashlib

import sys

import pytest

from scripts import lab_check_inputs as inputs


@pytest.mark.parametrize("name", inputs.EXTRA_TEST_INPUTS)
def test_required_input_missing_is_not_silently_skipped(tmp_path, name):
    for member in inputs.EXTRA_TEST_INPUTS:
        if member != name:
            path = tmp_path / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"trusted input")
    with pytest.raises((ValueError, FileNotFoundError)):
        inputs.trusted_test_inputs(tmp_path)


def test_overlay_copies_only_required_public_files_and_preserves_candidate(tmp_path):
    trusted, work, exact = (tmp_path / name for name in ("trusted", "work", "exact"))
    for root in (trusted, work, exact):
        root.mkdir()
    for name in inputs.EXTRA_TEST_INPUTS:
        source = trusted / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"trusted " + name.encode())
    (work / "candidate.py").write_bytes(b"candidate runtime")
    (exact / "candidate.py").write_bytes(b"candidate runtime")
    result = inputs.overlay_test_inputs(trusted, work)
    assert result == inputs.trusted_test_inputs(trusted)
    for name, digest in result.items():
        assert hashlib.sha256((work / name).read_bytes()).hexdigest() == digest
        assert not (exact / name).exists()
    assert (work / "candidate.py").read_bytes() == b"candidate runtime"
    assert list(exact.iterdir()) == [exact / "candidate.py"]


@pytest.mark.parametrize("mutation", ["missing", "extra", "overlay", "controller", "digest"])
def test_manifest_requires_exact_trusted_controller_overlay_correspondence(mutation):
    hashes = {name: hashlib.sha256(name.encode()).hexdigest() for name in inputs.EXTRA_TEST_INPUTS}
    manifest = {"trusted_test_inputs": dict(hashes), "controller": dict(hashes), "overlay": dict(hashes)}
    inputs.validate_test_inputs(manifest)
    name = inputs.EXTRA_TEST_INPUTS[0]
    if mutation == "missing":
        manifest["trusted_test_inputs"].pop(name)
    elif mutation == "extra":
        manifest["trusted_test_inputs"]["private/heldout.json"] = "0" * 64
    elif mutation == "digest":
        for mapping in manifest.values():
            mapping[name] = "not-sha256"
    else:
        manifest[mutation][name] = "0" * 64
    with pytest.raises(ValueError, match="[Tt]rusted"):
        inputs.validate_test_inputs(manifest)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux full-check controller/export primitives")
def test_real_full_check_path_supplies_trusted_inputs(tmp_path, monkeypatch):
    """The controller must bind the public fixtures before any worker starts."""
    import subprocess
    from scripts import lab_experiment as lab
    from tests.test_lab_experiment import Harness

    real_identity = lab.controller_identity
    harness = Harness(tmp_path, monkeypatch)
    monkeypatch.setattr(lab, "controller_identity", real_identity)
    seen = []

    def isolated(args, **kwargs):
        import json
        manifest = lab.read_json(kwargs["inputs"] / "inputs.json")
        inputs.validate_test_inputs(manifest)
        assert lab.sha(manifest["controller"]) == lab.sha(lab.controller_identity())
        for name in inputs.EXTRA_TEST_INPUTS:
            assert (kwargs["work"] / name).is_file()
            assert not (kwargs["inputs"] / "exact-candidate" / name).exists()
        seen.append(args)
        for suffix in (".output.json", ".cleanup.json", ".stdout", ".stderr"):
            kwargs["prefix"].with_suffix(suffix).write_text(json.dumps({"synthetic": True}))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(lab, "isolated_check", isolated)
    report = lab.full_candidate_checks(attempt=tmp_path / "checks", repo=harness.repo,
        revision=harness.initial, image=harness.config["image"], name="synthetic-input-check",
        check_tools=harness.campaign / "check-tools", check_tools_sha256=harness.config["check_tools_sha256"])
    assert report["passed"] and len(seen) == 3
    assert report["exact_candidate_sha256"] != report["overlay_sha256"]
