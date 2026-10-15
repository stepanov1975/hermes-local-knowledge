"""Synthetic controls only; never copy private benchmark questions into Git."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.lab_evaluator import load_benchmark, score, verdict


def fixture(tmp_path: Path) -> tuple[Path, dict]:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    mapping = [{"id": "native-a", "logical_id": "a"}, {"id": "native-b", "logical_id": "b"}]
    (corpus / "mapping.json").write_text(json.dumps(mapping))
    (corpus / "manifest.json").write_text(json.dumps({"inputs": {"mapping.json": {
        "sha256": hashlib.sha256((corpus / "mapping.json").read_bytes()).hexdigest(),
        "size": (corpus / "mapping.json").stat().st_size}}}))
    sha = hashlib.sha256((corpus / "manifest.json").read_bytes()).hexdigest()
    cases: list[dict] = [{"case_id": str(i), "origin": "a", "question": "question " + str(i),
              "intent": "goal", "targets": [{"logical_id": "b"}]} for i in range(2)]
    cases.sort(key=lambda c: (hashlib.sha256((c["origin"] + "\n" + c["case_id"]).encode()).hexdigest(), c["case_id"]))
    policy = {"schema": "source-appropriate-coverage-minima/v1", "corpus_manifest_sha256": sha,
              "origins": [{"origin": "a", "prospective_quota": 2}, {"origin": "b", "prospective_quota": 0}]}
    pp = tmp_path / "policy.json"
    pp.write_text(json.dumps(policy))
    ph = hashlib.sha256(pp.read_bytes()).hexdigest()
    frozen = {"schema": "private-baseline-plus-coverage-supplement/v1", "composite_id": "composite",
              "corpus_manifest_sha256": sha, "policy_sha256": ph,
              "development": cases[:1], "validation": cases[1:],
              "coverage": [{"origin": "a", "actual": 2, "minimum": 2, "development": 1, "validation": 1},
                           {"origin": "b", "actual": 0, "minimum": 0, "development": 0, "validation": 0}],
              "zero_query_origins": ["b"], "no_heldout_origins": ["b"]}
    fp = tmp_path / "frozen.json"
    fp.write_text(json.dumps(frozen))
    dev = {"schema": "worker-development-only/v1", "identity": "composite", "corpus_identity": sha,
           "cases": [{k: c[k] for k in ("case_id", "origin", "question", "intent", "targets")} for c in cases[:1]]}
    dp = tmp_path / "development.json"
    dp.write_text(json.dumps(dev))
    return corpus, {"frozen": fp, "frozen_sha256": hashlib.sha256(fp.read_bytes()).hexdigest(),
                    "policy": pp, "policy_sha256": ph, "development": dp,
                    "development_sha256": hashlib.sha256(dp.read_bytes()).hexdigest(),
                    "composite_id": "composite", "manifest_sha256": sha}


def test_coverage_aware_freeze_and_private_partition(tmp_path):
    corpus, args = fixture(tmp_path)
    benchmark = load_benchmark(corpus=corpus, **args)
    assert len(benchmark["development"]) == len(benchmark["validation"]) == 1
    assert "validation" not in json.loads(args["development"].read_text())


@pytest.mark.parametrize("mutation", ["partition", "mapping", "source", "duplicate", "development"])
def test_reject_changed_or_ambiguous_freeze(tmp_path, mutation):
    corpus, args = fixture(tmp_path)
    f = json.loads(args["frozen"].read_text())
    if mutation == "partition":
        f["development"], f["validation"] = f["validation"], f["development"]
    elif mutation == "duplicate":
        f["validation"] = f["development"]
    elif mutation == "mapping":
        p = corpus / "mapping.json"
        p.write_text(p.read_text().replace("native-b", "native-a"))
    elif mutation == "source":
        (corpus / "unapproved").write_text("drift")
    else:
        args["development"].write_text("{}")
    args["frozen"].write_text(json.dumps(f))
    args["frozen_sha256"] = hashlib.sha256(args["frozen"].read_bytes()).hexdigest()
    with pytest.raises((ValueError, AssertionError)):
        load_benchmark(corpus=corpus, **args)


def metrics(hit5=1, hit1=1, p95=0.02):
    return {"development": {"count": 2, "hit_at_5": hit5, "hit_at_1": hit1, "origin_hit_at_5": 0},
            "validation": {"count": 2, "hit_at_5": hit5, "hit_at_1": hit1, "origin_hit_at_5": 0},
            "warm_p95_seconds": p95, "errors": 0, "integrity": True, "trusted_tests": True,
            "official_integration": True, "complete": True, "benchmark_identity": "same"}


@pytest.mark.parametrize("control,eligible", [("improvement", True), ("equal", False),
    ("top1", False), ("latency", False), ("error", False), ("source", False),
    ("tests", False), ("identity", False)])
def test_score_eligibility_controls(control, eligible):
    old = metrics()
    new = metrics(hit5=2)
    if control == "equal":
        new = copy.deepcopy(old)
    elif control == "top1":
        new["development"]["hit_at_1"] = 0
    elif control == "latency":
        new["warm_p95_seconds"] = 1.001
    elif control == "error":
        new["errors"] = 1
    elif control == "source":
        new["integrity"] = False
    elif control == "tests":
        new["trusted_tests"] = False
    elif control == "identity":
        new["benchmark_identity"] = "different"
    assert verdict(old, new)["score_eligible"] is eligible
    assert verdict(old, new)["promoted"] is False


def test_incomplete_smoke_cannot_be_score_eligible():
    old, new = metrics(), metrics(hit5=2)
    new["complete"] = False
    assert not verdict(old, new)["score_eligible"]


def test_worker_export_rejects_hidden_partition_even_if_re_pinned(tmp_path):
    corpus, args = fixture(tmp_path)
    development = json.loads(args["development"].read_text())
    development["validation"] = [{"question": "private validation question"}]
    args["development"].write_text(json.dumps(development))
    args["development_sha256"] = hashlib.sha256(args["development"].read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="Unexpected fields"):
        load_benchmark(corpus=corpus, **args)


def test_multiple_unchanged_target_citations_are_not_ambiguous_mapping(tmp_path):
    corpus, args = fixture(tmp_path)
    frozen = json.loads(args["frozen"].read_text())
    frozen["validation"][0]["targets"] *= 2
    args["frozen"].write_text(json.dumps(frozen))
    args["frozen_sha256"] = hashlib.sha256(args["frozen"].read_bytes()).hexdigest()
    assert load_benchmark(corpus=corpus, **args)["validation"][0]["targets"] == frozen["validation"][0]["targets"]


def test_nearest_rank_p95_and_one_second_inclusive_boundary():
    from scripts.lab_evaluator import percentile95
    assert percentile95([float(n) for n in range(1, 21)]) == 19
    assert verdict(metrics(), metrics(hit5=2, p95=1))["score_eligible"]
    for value in (float("nan"), float("inf"), -1):
        assert not verdict(metrics(), metrics(hit5=2, p95=value))["score_eligible"]


def test_candidate_exports_never_include_comparator_even_in_future_revisions(tmp_path):
    from scripts.lab_evaluator_runtime import candidate_export
    for subdir in ("work/plugin", "home/plugins/local_knowledge"):
        root = tmp_path / subdir
        for relative in ("scripts/lab_evaluator.py", "scripts/lab_evaluator_runtime.py", "scripts/lab_evaluator_probe.py",
                         "tests/test_lab_evaluator.py", "docs/autonomous-lab-evaluator.md", "hermes_local_knowledge/plugin.py"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unchanged runtime or controller fixture")
    excluded = candidate_export(tmp_path)
    for subdir in ("work/plugin", "home/plugins/local_knowledge"):
        assert all(not (tmp_path / subdir / relative).exists() for relative in excluded)
        assert (tmp_path / subdir / "hermes_local_knowledge/plugin.py").read_text() == "unchanged runtime or controller fixture"


def test_evaluator_mounts_do_not_expose_controller_or_benchmark(tmp_path):
    from scripts.lab_evaluator_runtime import command
    corpus, _ = fixture(tmp_path)
    baseline, probe, tests = (tmp_path / n for n in ("baseline.py", "probe.py", "trusted-tests"))
    args = command("lk-lab-evaluation-control", "sha256:" + "a" * 64,
                   tmp_path, corpus, baseline, probe, tests)
    mounts = [args[i + 1] for i, x in enumerate(args) if x == "--mount"]
    assert len(mounts) == 7
    assert args[args.index("--network") + 1] == "none"
    assert all("frozen.json" not in m and "lab_evaluator.py" not in m and "policy.json" not in m for m in mounts)
    assert all(m.endswith(",readonly") for m in mounts if "dst=/trusted/" in m or "dst=/work/plugin/tests" in m)
    assert str(tmp_path) + ",dst=" not in " ".join(mounts)


def test_evaluator_spec_extension_preserves_original_resource_and_mount_checks(tmp_path):
    from scripts.lab_evaluator_runtime import verify_spec
    from scripts.lab_corpus_baseline import digest
    corpus, _ = fixture(tmp_path)
    baseline, probe, tests = (tmp_path / n for n in ("baseline.py", "probe.py", "trusted-tests"))
    spec: dict = {
        "Config": {"User": "10000:10000", "Labels": {"lk.lab": "ticket-66"}, "OpenStdin": True,
            "Entrypoint": ["/opt/hermes/.venv/bin/python"], "Cmd": ["/trusted/probe.py"],
            "Env": ["LAB_CORPUS_MANIFEST_SHA256=" + digest(corpus / "manifest.json")]},
        "HostConfig": {"Privileged": False, "CapAdd": None, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"], "ReadonlyRootfs": True, "NetworkMode": "none",
            "Memory": 2147483648, "MemorySwap": 2147483648, "NanoCpus": 2000000000, "PidsLimit": 256},
        "Mounts": [{"Type": "bind", "Source": str(p), "Destination": dst, "RW": rw} for p, dst, rw in (
            (tmp_path / "home", "/opt/data", True), (tmp_path / "work", "/work", True),
            (tmp_path / "output", "/output", True), (corpus, "/corpus", False),
            (baseline, "/trusted/baseline.py", False), (probe, "/trusted/probe.py", False),
            (tests, "/work/plugin/tests", False))]}
    verify_spec(spec, tmp_path, corpus, baseline, probe, tests)
    for mutation in ("privileged", "extra_mount", "writable_probe"):
        changed = copy.deepcopy(spec)
        if mutation == "privileged":
            changed["HostConfig"]["Privileged"] = True
        elif mutation == "extra_mount":
            changed["Mounts"].append({"Type": "bind", "Source": "/", "Destination": "/host", "RW": False})
        else:
            changed["Mounts"][-2]["RW"] = True
        with pytest.raises((ValueError, AssertionError)):
            verify_spec(changed, tmp_path, corpus, baseline, probe, tests)


def test_actual_top5_not_origin_or_neighbors_and_controller_timing():
    cases = [{"case_id": "x", "origin": "a", "targets": [{"logical_id": "b"}]}]
    result = score(cases, [{"results": [{"id": "native-a"}, {"id": "native-b"}]}], [0.1],
                   {"native-a": "a", "native-b": "b"})
    assert result == {"count": 1, "hit_at_5": 1, "hit_at_1": 0, "origin_hit_at_5": 1}


@pytest.mark.parametrize("response", [{"error": "failed"}, {"results": "bad"},
    {"results": [{"id": "unknown"}]}, {"results": [{"id": "native-a"}] * 2},
    {"results": [{}]}, {"results": [{"id": "native-a"}] * 6}])
def test_malformed_native_output_cannot_score(response):
    with pytest.raises(ValueError):
        score([{"origin": "a", "targets": [{"logical_id": "b"}]}], [response], [0.1], {"native-a": "a"})


@pytest.fixture
def stdout_child():
    """Real pipe transport; terminate producers on expected reader refusal."""
    from contextlib import contextmanager
    import subprocess
    import sys

    @contextmanager
    def child(code):
        process = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.PIPE)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
    return child


def test_frame_noise_budget_is_cumulative_across_valid_responses(stdout_child):
    from scripts.lab_evaluator_runtime import Frames
    code = ("import os, json\n"
            "for i in range(8):\n"
            " os.write(1, (b'x'*1023+b'\\n')*1024)\n"
            " print('LK68 '+json.dumps({'part':i}), flush=True)\n")
    with stdout_child(code) as process:
        frames = Frames(process)
        for i in range(4):
            assert frames.read() == {"part": i}
        with pytest.raises(ValueError, match="diagnostic"):
            frames.read()
        assert len(frames.noise) <= 4 * 1024 * 1024


def test_final_stdout_drain_is_bounded(stdout_child):
    from scripts.lab_evaluator_runtime import Frames
    code = ("import os\nprint('LK68 {\"ready\":true}', flush=True)\n"
            "os.write(1, (b'y'*1023+b'\\n')*8192)\n")
    with stdout_child(code) as process:
        frames = Frames(process)
        assert frames.read() == {"ready": True}
        with pytest.raises(ValueError, match="diagnostic"):
            frames.finish()
        assert len(frames.noise) <= 4 * 1024 * 1024


def test_final_stdout_preserves_buffered_and_trailing_diagnostics(stdout_child):
    from scripts.lab_evaluator_runtime import Frames
    code = "import os; os.write(1, b'before\\nLK68 {\"ready\":true}\\nafter-without-newline')"
    with stdout_child(code) as process:
        frames = Frames(process)
        assert frames.read() == {"ready": True}
        assert frames.finish() == b"before\n" + b"after-without-newline"
        assert process.wait() == 0



def test_final_drain_shares_prior_read_diagnostic_allowance(stdout_child):
    from scripts.lab_evaluator_runtime import Frames
    code = ("import os\n"
            "os.write(1, (b'z'*1023+b'\\n')*3072)\n"
            "print('LK68 {\"ready\":true}', flush=True)\n"
            "os.write(1, (b'z'*1023+b'\\n')*2048)\n")
    with stdout_child(code) as process:
        frames = Frames(process)
        assert frames.read() == {"ready": True}
        assert len(frames.noise) == 3 * 1024 * 1024
        with pytest.raises(ValueError, match="diagnostic"):
            frames.finish()
        assert len(frames.noise) <= 4 * 1024 * 1024
