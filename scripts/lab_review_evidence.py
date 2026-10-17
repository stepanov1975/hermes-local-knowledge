"""Narrow reviewer projection and full-check receipt packet (no private case data)."""
from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
import re
import shutil
import stat
from typing import Any

POLICY = "lab_agent.permitted/v1"
SCOPE = "tests/test_config.py::test_verified_routing_defaults_are_off_and_immutable"
COMMAND = "/opt/lab-tools/bin/python -m pytest -q -p no:cacheprovider " + SCOPE
CHECKS = ("full_pytest", "ruff", "mypy")
PROOF_FILES = {"checks.json", "inputs.json"} | {
    name + suffix for name in CHECKS
    for suffix in (".output.json", ".cleanup.json", ".stdout", ".stderr")}


INPUTS_SNAPSHOT_CONTRACT = (
    "check-inputs snapshot: map exact-candidate/<path> to inputs.json.candidate[<path>], "
    "tools/<path> to inputs.json.tools[<path>], and inputs.json to SHA256 of its raw sidecar bytes. "
    "inputs_sha256 is SHA256 of UTF-8 (json.dumps(map, sort_keys=True, separators=(',', ':'), "
    "allow_nan=False, ensure_ascii=True) + '\\n'); inputs_manifest_sha256 is only the raw inputs.json SHA256."
)


def validate_commit_object(value: dict, revision: str, tree: str) -> None:
    from scripts.lab_evaluator import require
    require(value.get("kind") == "commit", "Commit object kind mismatch")
    encoded = value.get("body_base64")
    if not isinstance(encoded, str):
        raise ValueError("Malformed commit object encoding")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Malformed commit object encoding") from exc
    require(base64.b64encode(body).decode("ascii") == encoded, "Noncanonical commit object encoding")
    headers = body.split(b"\n\n", 1)[0].split(b"\n")
    require(b"\0" not in body and b"\n\n" in body
            and bool(re.fullmatch(rb"tree [0-9a-f]{40}", headers[0]))
            and any(h.startswith(b"author ") for h in headers)
            and any(h.startswith(b"committer ") for h in headers), "Malformed commit object body")
    oid = hashlib.sha1(b"commit " + str(len(body)).encode("ascii") + b"\0" + body).hexdigest()
    require(oid == revision and headers[0][5:].decode("ascii") == tree, "Commit object revision/tree mismatch")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def projection(repo: Path, revision: str) -> dict[str, Any]:
    from scripts import lab_agent as agent, lab_experiment as lab
    lab.revision_exists(repo, revision)
    admitted, omitted = {}, {}
    for name, (mode, oid) in lab.tracked(repo, revision).items():
        item = {"git_mode": mode, "sha256": digest(lab.git(repo, "cat-file", "blob", oid)), "git_oid": oid}
        if agent.permitted(name):
            lab.require(mode in {"100644", "100755"}, "Unsafe admitted Git member")
            admitted[name] = item
        else:
            omitted[name] = {**item, "reason": "excluded by positive export policy"}
    return {"revision": revision, "tree": lab.git(repo, "rev-parse", revision + "^{tree}").decode().strip(),
            "policy": POLICY, "policy_source_sha256": lab.file_sha(Path(agent.__file__)),
            "admitted": admitted, "omitted": omitted,
            "scope": "Filtered export, NOT a complete Git tree; omitted code bytes are not supplied."}


def validate_projection(workspace: Path, manifest: dict) -> None:
    from scripts.lab_evaluator import require
    actual = {}
    for path in workspace.rglob("*"):
        mode = path.lstat().st_mode
        require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), "Unsafe projection member")
        if stat.S_ISREG(mode):
            actual[path.relative_to(workspace).as_posix()] = {
                "sha256": digest(path.read_bytes()), "git_mode": "100" + format(stat.S_IMODE(mode), "03o")}
    expected = {name: {k: item[k] for k in ("sha256", "git_mode")}
                for name, item in manifest["admitted"].items()}
    require(actual == expected, "Reviewer projection bytes/modes mismatch")


def regular(root: Path, name: str) -> Path:
    from scripts.lab_evaluator import require
    path = root / name
    require(not root.is_symlink() and root.is_dir() and Path(name).name == name,
            "Unsafe proof path")
    require(stat.S_ISREG(path.lstat().st_mode), "Proof must be a regular file")
    return path


def file_receipt(root: Path, name: str) -> dict:
    path = regular(root, name)
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(256 * 1024):
            sha.update(chunk)
    return {"sha256": sha.hexdigest(), "size": path.stat().st_size}


def receipt_files(root: Path, name: str) -> dict:
    return {name + suffix: file_receipt(root, name + suffix)
            for suffix in (".output.json", ".cleanup.json", ".stdout", ".stderr")}


def inventory(root: Path) -> dict:
    from scripts.lab_evaluator import require
    require({p.name for p in root.iterdir()} == PROOF_FILES, "Unexpected/missing proof members")
    return {name: file_receipt(root, name) for name in sorted(PROOF_FILES)}


def validate(packet: dict, proof: Path, *, revision: str, image: str) -> None:
    from scripts import lab_experiment as lab
    lab.require(packet.get("schema") == "lab-review-evidence/v2", "Review evidence schema mismatch")
    binding = packet["binding"]
    manifest = packet["projection"]
    lab.require(binding["revision"] == manifest["revision"] == revision
                and binding["tree"] == manifest["tree"] and binding["image"] == image,
                "Review evidence identity mismatch")
    validate_commit_object(packet["commit_object"], revision, binding["tree"])
    lab.require(lab.sha(manifest) == packet["projection_sha256"], "Projection manifest mismatch")
    lab.require(inventory(proof) == packet["proof_files"], "Review proof hash/size mismatch")
    checks = lab.read_json(proof / "checks.json")
    inputs = lab.read_json(proof / "inputs.json")
    lab.require(lab.sha(checks) == binding["checks_sha256"] and checks["revision"] == revision
                and checks["image"] == image and checks["controller"] == binding["controller"]
                and inputs["revision"] == revision and checks["cleanup_complete"] is True
                and checks["inputs_manifest_sha256"] == lab.file_sha(proof / "inputs.json"),
                "Full-check identity mismatch")
    all_members = {**manifest["admitted"], **manifest["omitted"]}
    lab.require(inputs["candidate"] == {n: v["sha256"] for n, v in all_members.items()},
                "Full-check candidate manifest mismatch")
    lab.require(checks["check_tools_sha256"] == inputs["check_tools_sha256"] == lab.sha(inputs["tools"]),
                "Check-tool identity mismatch")
    lab.require(all(inputs["overlay"].get(n) == v["sha256"] for n, v in all_members.items()
                    if n.startswith("hermes_local_knowledge/")), "Overlay changed candidate runtime bytes")
    lab.require(packet.get("inputs_snapshot_contract") == INPUTS_SNAPSHOT_CONTRACT,
                "Input snapshot contract mismatch")
    snapshot = {"exact-candidate/" + n: h for n, h in inputs["candidate"].items()}
    snapshot.update({"tools/" + n: h for n, h in inputs["tools"].items()})
    snapshot["inputs.json"] = lab.file_sha(proof / "inputs.json")
    lab.require(checks["inputs_sha256"] == lab.sha(snapshot), "Input snapshot digest mismatch")
    # Candidate and overlay map digests are distinct from the composite input snapshot.
    lab.require(checks["exact_candidate_sha256"] == lab.sha(inputs["candidate"])
                and checks["overlay_sha256"] == lab.sha(inputs["overlay"]), "Check-input digest mismatch")
    lab.require({c["name"] for c in checks["checks"]} == set(CHECKS)
                and len(checks["checks"]) == len(CHECKS), "Incomplete full checks")
    for check in checks["checks"]:
        name = check["name"]
        lab.require(check["evidence_files"] == receipt_files(proof, name), "Check receipt binding mismatch")
        lab.require(isinstance(check["command"], list) and bool(check["command"]), "Missing check command")
        lab.require(check["output_receipt"] == name + ".output.json"
                    and check["cleanup_receipt"] == name + ".cleanup.json", "Unexpected check receipt")
        output = lab.read_json(proof / check["output_receipt"])
        cleanup = lab.read_json(proof / check["cleanup_receipt"])
        lab.require(output["exit_code"] == check["returncode"]
                    and check["passed"] == (check["returncode"] == 0)
                    and output["output_limit_exceeded"] is False and output["error_class"] is None
                    and cleanup["cleanup_complete"] is True and bool(cleanup["container_id"]),
                    "Incomplete/contradictory check receipt")
        lab.require(output["args"] == ["docker", "start", "--attach", cleanup["container_id"]],
                    "Check worker identity mismatch")
        for stream in ("stdout", "stderr"):
            lab.require(output["files"][stream] == name + "." + stream
                        and output["stored_bytes"][stream] == packet["proof_files"][name + "." + stream]["size"],
                        "Check output size mismatch")
    lab.require(checks["passed"] == all(c["passed"] for c in checks["checks"]), "Contradictory check summary")
    lab.require(packet["runnable_scope"]["command"] == COMMAND
                and "tests/test_config.py" in manifest["admitted"], "Missing runnable review scope")


def build(*, repo: Path, revision: str, binding: dict, report: dict, checks_root: Path, proof: Path) -> dict:
    """Copy only named safe receipts; never mount their parent or source/hidden overlay."""
    from scripts import lab_experiment as lab
    proof.mkdir(mode=0o755)
    proof.chmod(0o755)  # The read-only worker uses a different UID, even under umask 077.
    for name in sorted(PROOF_FILES):
        source = checks_root / "check-inputs" if name == "inputs.json" else checks_root
        shutil.copyfile(regular(source, name), proof / name)
        (proof / name).chmod(0o444)
    manifest = projection(repo, revision)
    packet = {"schema": "lab-review-evidence/v2", "binding": binding, "draft_report": report,
              "commit_object": {"kind": "commit", "body_base64": base64.b64encode(
                  lab.git(repo, "cat-file", "commit", revision)).decode("ascii")},
              "inputs_snapshot_contract": INPUTS_SNAPSHOT_CONTRACT,
              "projection": manifest, "projection_sha256": lab.sha(manifest), "proof_files": inventory(proof),
              "proof_directory": "/inputs/review-proof",
              "trusted_full_checks": "Full Git source plus trusted tests/scripts/pyproject overlay; inputs.json binds both maps. "
                  "commit_object contains strict base64 raw commit body: SHA1(b'commit ' + str(len(body)).encode('ascii') + "
                  "b'\\0' + body) must equal revision, and its first tree header must equal the bound tree. "
                  "Full trusted checks are not reproducible from this partial projection. Overlay code and hidden/controller files remain excluded. "
                  "Receipts are controller-supplied evidence, not independent attestation. Challenge contents and correspondence. "
                  "Output JSON stdout/stderr fields are incomplete previews; complete bounded raw streams are sidecars.",
              "runnable_scope": {"command": COMMAND, "scope": "One admitted config default test, not the full suite.",
                  "full_suite": "Not runnable: admitted tests can import omitted scripts; do not reconstruct the full tree from this export."}}
    validate(packet, proof, revision=revision, image=binding["image"])
    return packet


def prepare(evidence: Path, attempt: Path, workspace: Path, repo: Path, revision: str, image: str) -> dict:
    from scripts import lab_experiment as lab
    packet = lab.read_json(evidence)
    source = evidence.parent / "review-proof"
    validate(packet, source, revision=revision, image=image)
    lab.require(packet["projection"] == projection(repo, revision), "Review projection provenance mismatch")
    validate_projection(workspace, packet["projection"])
    destination = attempt / "inputs/review-proof"
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    for name in sorted(PROOF_FILES):
        shutil.copyfile(regular(source, name), destination / name)
        (destination / name).chmod(0o444)
    validate(packet, destination, revision=revision, image=image)
    return packet
