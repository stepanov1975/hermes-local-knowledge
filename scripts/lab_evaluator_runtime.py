"""Offline exact-revision Docker execution using #65/#66 isolation primitives."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time
from typing import Any

from scripts.lab_corpus_baseline import baseline_command, corpus_integrity, digest, verify_baseline_spec
from scripts.lab_evaluator import PROTOCOL, WARMUPS, percentile95, private_report, require, score
from scripts.lab_runtime import prepare_attempt, run, scoped_name, validate_pin


class Frames:
    """Bounded individual reads, never a whole-experiment elapsed allowance."""

    MAX_DIAGNOSTIC_BYTES = 4 * 1024 * 1024

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.buffer = b""
        self.noise = bytearray()

    def retain(self, data: bytes) -> None:
        require(len(self.noise) + len(data) <= self.MAX_DIAGNOSTIC_BYTES,
                "Cumulative native diagnostic allowance exceeded")
        self.noise.extend(data)

    def read(self) -> dict:
        assert self.process.stdout is not None
        deadline = time.monotonic() + 300
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while True:
                if b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    if line.startswith(b"LK68 "):
                        return json.loads(line[5:])
                    self.retain(line + b"\n")
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Native transport readiness/read timeout; no query retry")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Native transport ended before response")
                self.buffer += chunk
                require(len(self.buffer) <= 4 * 1024 * 1024, "Oversized native frame")

    def finish(self) -> bytes:
        """Drain EOF diagnostics under the same cumulative memory allowance."""
        assert self.process.stdout is not None
        self.retain(self.buffer)
        self.buffer = b""
        deadline = time.monotonic() + 300
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Native final transport drain timeout")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    return bytes(self.noise)
                self.retain(chunk)


def tree_hashes(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*") if p.is_file()}


def candidate_export(attempt: Path) -> list[str]:
    """Remove controller-only files, never change the Git-selected runtime package."""
    excluded = ["scripts/lab_evaluator.py", "scripts/lab_evaluator_runtime.py", "scripts/lab_evaluator_probe.py",
                "tests/test_lab_evaluator.py", "docs/autonomous-lab-evaluator.md"]
    for root in (attempt / "work/plugin", attempt / "home/plugins/local_knowledge"):
        for relative in excluded:
            (root / relative).unlink(missing_ok=True)
    return excluded


def command(name: str, image: str, attempt: Path, corpus: Path, baseline: Path,
            probe: Path, tests: Path) -> list[str]:
    result = baseline_command(name, image, attempt, corpus, baseline)
    result.insert(2, "--interactive")
    result[-1] = "/trusted/probe.py"
    position = result.index("--entrypoint")
    result[position:position] = ["--mount", f"type=bind,src={probe},dst=/trusted/probe.py,readonly",
                                 "--mount", f"type=bind,src={tests},dst=/work/plugin/tests,readonly"]
    return result


def verify_spec(spec: dict, attempt: Path, corpus: Path, baseline: Path, probe: Path, tests: Path) -> None:
    # Normalize ONLY these two explicitly pinned read-only mounts and command.
    # The existing #66 checker still enforces the original four-bind boundary.
    checked = json.loads(json.dumps(spec))
    for source, target in ((probe, "/trusted/probe.py"), (tests, "/work/plugin/tests")):
        rows = [m for m in checked["Mounts"] if m["Destination"] == target]
        require(len(rows) == 1 and rows[0]["Source"] == str(source.resolve()) and not rows[0]["RW"]
                and rows[0]["Type"] == "bind", "Unapproved evaluator mount")
        checked["Mounts"].remove(rows[0])
    require(checked["Config"]["Cmd"] == ["/trusted/probe.py"] and checked["Config"]["OpenStdin"],
            "Unapproved evaluator command")
    checked["Config"]["Cmd"] = ["/trusted/baseline.py"]
    verify_baseline_spec(checked, attempt, corpus, baseline)


def evaluate_revision(*, attempt: Path, repo: Path, revision: str, corpus: Path, image: str,
                      name: str, benchmark: dict, smoke: int | None = None) -> dict:
    scoped_name(name)
    validate_pin(image)
    # Fail before any launch if the immutable pre-existing runtime is unavailable.
    image_spec = json.loads(run(["docker", "image", "inspect", image]))[0]
    require(image_spec["Config"]["Labels"].get("org.opencontainers.image.revision") ==
            "90194c64ff99933ec3b9ecfd9295b06e6c11e038", "Unexpected official runtime provenance")
    require(digest(corpus / "manifest.json") == benchmark["corpus_manifest_sha256"], "Benchmark source drift")
    manifest = corpus_integrity(corpus)
    prepare_attempt(attempt, repo, revision)
    excluded = candidate_export(attempt)
    attempt = attempt.resolve()
    corpus = corpus.resolve()
    config_path = attempt / "home/config.yaml"
    cfg = json.loads(config_path.read_text())
    cfg["local_knowledge"].update(manifest["settings"])
    cfg["local_knowledge"].update(source_root="/corpus/source", hermes_home="/corpus/scanner")
    config_path.write_text(json.dumps(cfg, sort_keys=True, indent=2) + "\n")
    config_hash = digest(config_path)
    source_hashes = {d: tree_hashes(attempt / d) for d in ("work/plugin", "home/plugins/local_knowledge")}
    baseline = Path(__file__).with_name("lab_corpus_baseline.py").resolve()
    probe = Path(__file__).with_name("lab_evaluator_probe.py").resolve()
    tests = attempt / "trusted-tests"
    tests.mkdir()
    # Only public contract tests: never expose evaluator tests or comparison rules.
    for filename in ("conftest.py", "test_public_contract.py", "test_plugin.py"):
        shutil.copyfile(Path(__file__).resolve().parents[1] / "tests" / filename, tests / filename)
    for path in tests.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    tests.chmod(0o555)
    trusted_hashes = tree_hashes(tests)
    setup = ["docker", "run", "--rm", "--name", name + "-setup", "--label", "lk.lab=ticket-66",
             "--network", "none", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
             "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges:true", "--read-only",
             "--memory", "64m", "--cpus", "0.25", "--pids-limit", "16", "--entrypoint", "/bin/chown"]
    for child in ("home", "work", "output"):
        setup += ["--mount", f"type=bind,src={attempt / child},dst=/{child}"]
    # No candidate code runs in this narrowly privileged existing setup helper.
    run(setup + [image, "-R", "10000:10000", "/home", "/work", "/output"])
    private_report(attempt / "image.json", image_spec)
    private_report(attempt / "runner.json", {"protocol": PROTOCOL, "revision": revision,
        "image": image, "excluded_controller_paths": excluded,
        "baseline_sha256": digest(baseline), "probe_sha256": digest(probe),
        "controller_sha256": digest(Path(__file__)), "comparator_sha256": digest(Path(__file__).with_name("lab_evaluator.py")),
        "trusted_tests": trusted_hashes})
    worker = name + "-worker"
    run(command(worker, image, attempt, corpus, baseline, probe, tests))
    process: Any = None
    frames: Frames | None = None
    try:
        before = json.loads(run(["docker", "inspect", worker]))[0]
        verify_spec(before, attempt, corpus, baseline, probe, tests)
        require(before["Image"] == image_spec["Id"], "Runtime image mismatch")
        private_report(attempt / "worker-before.json", before)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        with (attempt / "transport.stderr").open("xb") as stderr:
            process = subprocess.Popen(["docker", "start", "--attach", "--interactive", worker],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, env=env)
            frames = Frames(process)
            ready = frames.read()
            mapping = {m["id"]: m["logical_id"] for m in json.loads((corpus / "mapping.json").read_text())}
            require(ready == {"ready": True, "mapping_count": len(mapping)}, "Incomplete native readiness")
            # Fixed trusted test selection, supplied by controller, never the candidate's tests.
            test_command = ["docker", "exec", "--env", "PYTHONPATH=/work/plugin", worker,
                "/opt/lab-tools/bin/python", "-m", "pytest", "tests/test_public_contract.py",
                "tests/test_plugin.py", "-q", "-p", "no:cacheprovider"]
            test_output = run(test_command, timeout=None)
            (attempt / "trusted-tests.log").write_text(test_output)
            sequence = 0

            def query(text: str) -> tuple[dict, float]:
                nonlocal sequence
                started = time.perf_counter_ns()
                process.stdin.write((json.dumps({"sequence": sequence, "query": text}) + "\n").encode())
                process.stdin.flush()
                frame = frames.read()
                elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
                require(set(frame) == {"sequence", "response"} and frame["sequence"] == sequence,
                        "Out-of-order native result")
                sequence += 1
                return frame["response"], elapsed

            warmups = []
            for warmup in WARMUPS:
                response, elapsed = query(warmup)
                warmups.append({"query": warmup, "response": response, "controller_seconds": elapsed})
                # Validate warmups too; an error cannot be hidden as unscored.
                score([{"origin": "", "targets": []}], [response], [elapsed], mapping)
            private_report(attempt / "warmups.json", {"unscored": warmups})
            report: dict[str, Any] = {"benchmark_identity": benchmark["composite_id"], "revision": revision,
                "image": image_spec["Id"], "protocol": PROTOCOL, "errors": 0, "trusted_tests": True}
            timings = []
            with (attempt / "private-cases.jsonl").open("x") as private:
                (attempt / "private-cases.jsonl").chmod(0o600)
                for split in ("development", "validation"):
                    cases = sorted(benchmark[split], key=lambda c: c["case_id"])
                    if smoke is not None:
                        cases = cases[:smoke]
                    responses, seconds = [], []
                    for case in cases:
                        response, elapsed = query(case["question"])
                        responses.append(response)
                        seconds.append(elapsed)
                        private.write(json.dumps({"split": split, "case_id": case["case_id"],
                            "response": response, "controller_seconds": elapsed}) + "\n")
                        private.flush()
                    report[split] = score(cases, responses, seconds, mapping)
                    for metric in ("hit_at_5", "hit_at_1", "origin_hit_at_5"):
                        report[split][metric + "_rate"] = report[split][metric] / len(cases)
                    timings.extend(seconds)
            process.stdin.close()
            # EOF lets #66 run its final source/seed/config/provenance checks.
            diagnostics = frames.finish()
            require(process.wait() == 0, "Native integration postchecks failed")
            (attempt / "transport.stdout").write_bytes(diagnostics)
        after = json.loads(run(["docker", "inspect", worker]))[0]
        verify_spec(after, attempt, corpus, baseline, probe, tests)
        require(after["State"]["ExitCode"] == 0, "Native worker failed")
        private_report(attempt / "worker-after.json", after)
        # Official Hermes secures config.yaml to 0600. Restore only this stopped
        # disposable export to its controller owner before host-side readback.
        run(setup + [image, "-R", f"{os.getuid()}:{os.getgid()}", "/home", "/work", "/output"])
        native = json.loads((attempt / "output/native-baseline.json").read_text())
        require(native["integrity"] == "ok" and native["logical_mapping_count"] == len(mapping), "Native mapping failed")
        require(digest(config_path) == config_hash and tree_hashes(tests) == trusted_hashes, "Execution config/test drift")
        require(all(tree_hashes(attempt / d) == hashes for d, hashes in source_hashes.items()), "Plugin source drift")
        corpus_integrity(corpus)
        report.update(integrity=True, official_integration=True, warm_p95_seconds=percentile95(timings),
                      scored_cases=len(timings), complete=smoke is None)
        private_report(attempt / "aggregate.json", report)
        return report
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        spec = json.loads(run(["docker", "inspect", worker]))[0]
        require(spec["Config"]["Labels"]["lk.lab"] == "ticket-66", "Cleanup ownership mismatch")
        run(["docker", "rm", "--force", "--volumes", worker])
        require(not run(["docker", "ps", "--all", "--filter", "name=^/" + worker + "$", "--format", "{{.ID}}"]).strip(),
                "Worker remains after removal")
        private_report(attempt / "cleanup.json", {"worker_removed": True})
        # Diagnostic persistence must never prevent owned-container cleanup.
        if frames is not None:
            (attempt / "transport.stdout").write_bytes(bytes(frames.noise))
