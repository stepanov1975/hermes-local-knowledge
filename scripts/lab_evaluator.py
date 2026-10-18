"""Controller-only coverage-aware scoring. Never mount this module in a candidate.

Protocol v1: development then validation, each ordered by case_id; three fixed
non-benchmark warmups; one public limit-five call per case; nearest-rank p95 of
controller wall times after warmup (transport included, index build excluded).
This returns score eligibility only, never independent review or promotion.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from scripts.lab_corpus_baseline import corpus_integrity, digest

PROTOCOL = "lk-coverage-public-search-v1"
WARMUPS = ("local knowledge evaluator warmup alpha", "local knowledge evaluator warmup beta",
           "local knowledge evaluator warmup gamma")


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def pinned(path: Path, expected: str) -> Any:
    require(digest(path) == expected, "Pinned file changed")
    return json.loads(path.read_text())


def load_benchmark(*, frozen: Path, frozen_sha256: str, policy: Path, policy_sha256: str,
                   development: Path, development_sha256: str, composite_id: str,
                   corpus: Path, manifest_sha256: str) -> dict:
    """Validate the new schema without relaxing historical #67 freeze validators."""
    require(digest(corpus / "manifest.json") == manifest_sha256, "Manifest pin differs")
    corpus_integrity(corpus)
    mapping = json.loads((corpus / "mapping.json").read_text())
    logical = {m["logical_id"] for m in mapping}
    require(len(logical) == len(mapping) == len({m["id"] for m in mapping}), "Ambiguous artifact mapping")
    f = pinned(frozen, frozen_sha256)
    p = pinned(policy, policy_sha256)
    d = pinned(development, development_sha256)
    require(f["schema"] == "private-baseline-plus-coverage-supplement/v1", "Unsupported benchmark schema")
    require(p["schema"] == "source-appropriate-coverage-minima/v1", "Unsupported coverage policy")
    require(d["schema"] == "worker-development-only/v1", "Unsupported development export")
    require(set(d) <= {"schema", "identity", "corpus_identity", "cases", "coverage"},
            "Unexpected fields in worker export")
    require(f["composite_id"] == d["identity"] == composite_id, "Composite identity differs")
    require(f["policy_sha256"] == policy_sha256, "Policy binding differs")
    require(f["corpus_manifest_sha256"] == p["corpus_manifest_sha256"] == d["corpus_identity"] == manifest_sha256,
            "Source identity differs")
    quotas = {r["origin"]: r["prospective_quota"] for r in p["origins"]}
    require(len(quotas) == len(p["origins"]) and set(quotas) == logical, "Policy origin coverage differs")
    by_origin: dict[str, list] = defaultdict(list)
    ids: set[str] = set()
    questions: set[str] = set()
    for split in ("development", "validation"):
        require(isinstance(f[split], list) and f[split], "Empty or malformed split")
        for c in f[split]:
            require(isinstance(c["case_id"], str) and c["case_id"] and c["case_id"] not in ids, "Duplicate or invalid case ID")
            require(isinstance(c["question"], str) and c["question"].strip() and c["question"] not in questions,
                    "Duplicate or malformed question")
            require(c["question"] not in WARMUPS, "Warmup collides with a scored question")
            require(c["origin"] in logical, "Unknown origin")
            require(isinstance(c["targets"], list) and c["targets"], "No acceptable targets")
            targets = [t["logical_id"] for t in c["targets"]]
            # Multiple evidence citations may name the same unchanged target.
            require(set(targets) <= logical, "Unknown acceptable target")
            ids.add(c["case_id"])
            questions.add(c["question"])
            by_origin[c["origin"]].append(c)
    actual_dev = {c["case_id"] for c in f["development"]}
    coverage = {r["origin"]: r for r in f["coverage"]}
    require(len(coverage) == len(f["coverage"]) and set(coverage) == logical, "Incomplete coverage table")
    for origin in logical:
        cases = sorted(by_origin[origin], key=lambda c: (
            hashlib.sha256((origin + "\n" + c["case_id"]).encode("utf-8")).hexdigest(), c["case_id"]))
        n = len(cases)
        quota = quotas[origin]
        require(type(quota) is int and quota >= 0 and n >= quota, "Coverage minimum not met")
        dev_n = min(3, max(1, n - 1)) if n else 0
        require({c["case_id"] for c in cases[:dev_n]} == actual_dev.intersection(c["case_id"] for c in cases),
                "Frozen partition differs from coverage-aware hash rule")
        require(coverage[origin] == {"origin": origin, "actual": n, "minimum": quota,
                                    "development": dev_n, "validation": n - dev_n}, "Coverage counts differ")
    require(set(f["zero_query_origins"]) == {o for o in logical if not by_origin[o]}, "Zero-origin disclosure differs")
    require(set(f["no_heldout_origins"]) == {o for o in logical if len(by_origin[o]) <= 1}, "Heldout disclosure differs")
    exported = [{k: c[k] for k in ("case_id", "intent", "origin", "question", "targets")} for c in f["development"]]
    require(d["cases"] == exported, "Development export differs or leaks another partition")
    if "coverage" in d:
        require(d["coverage"] == {"actual": len(ids), "minimum": sum(quotas.values()), "origins": len(logical),
            "zero_query_origins": len(f["zero_query_origins"]), "no_heldout_origins": len(f["no_heldout_origins"])},
            "Unexpected worker coverage metadata")
    return f


def score(cases: list[dict], responses: list[dict], seconds: list[float], mapping: dict[str, str]) -> dict:
    require(len(cases) == len(responses) == len(seconds) and bool(cases), "Incomplete scored split")
    result = {"count": len(cases), "hit_at_5": 0, "hit_at_1": 0, "origin_hit_at_5": 0}
    for case, response, elapsed in zip(cases, responses, seconds):
        require(isinstance(response, dict) and not response.get("error"), "Lookup error")
        rows = response.get("results")
        if not isinstance(rows, list) or len(rows) > 5:
            raise ValueError("Malformed top five")
        require(all(isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"] in mapping for r in rows),
                "Unmapped or malformed result")
        found = [mapping[r["id"]] for r in rows]
        require(len(set(found)) == len(found), "Duplicate result")
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, "Invalid controller timing")
        targets = {t["logical_id"] for t in case["targets"]}
        result["hit_at_5"] += int(bool(targets.intersection(found)))
        result["hit_at_1"] += int(bool(found) and found[0] in targets)
        result["origin_hit_at_5"] += int(case["origin"] in found)
    return result


def percentile95(seconds: list[float]) -> float:
    require(bool(seconds) and all(math.isfinite(s) and s >= 0 for s in seconds), "Invalid timing samples")
    return sorted(seconds)[math.ceil(len(seconds) * 0.95) - 1]


def verdict(incumbent: dict, candidate: dict) -> dict:
    reasons = []
    if incumbent["benchmark_identity"] != candidate["benchmark_identity"]:
        reasons.append("benchmark_identity")
    for name, report in (("incumbent", incumbent), ("candidate", candidate)):
        for gate in ("integrity", "trusted_tests", "official_integration", "complete"):
            if report.get(gate) is not True:
                reasons.append(name + ":" + gate)
        if report.get("errors") != 0:
            reasons.append(name + ":lookup_errors")
        latency = report.get("warm_p95_seconds")
        if not isinstance(latency, (int, float)) or isinstance(latency, bool) or not math.isfinite(latency) or not 0 <= latency <= 1:
            reasons.append(name + ":latency")
    for split in ("development", "validation"):
        old, new = incumbent[split], candidate[split]
        if old["count"] != new["count"] or old["count"] <= 0:
            reasons.append(split + ":case_counts")
        if new["hit_at_1"] < old["hit_at_1"]:
            reasons.append(split + ":top1_regression")
    if candidate["validation"]["hit_at_5"] <= incumbent["validation"]["hit_at_5"]:
        reasons.append("validation:not_strictly_improved")
    return {"score_eligible": not reasons, "reasons": reasons, "promoted": False,
            "independent_review_required": True}


def private_report(path: Path, payload: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(payload, stream, sort_keys=True, indent=2)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("corpus", "frozen", "policy", "development", "plugin-repo", "attempt"):
        parser.add_argument("--" + key, type=Path, required=True)
    for key in ("frozen-sha256", "policy-sha256", "development-sha256", "composite-id", "manifest-sha256",
                "incumbent", "candidate", "image", "name"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--smoke-cases-per-split", type=int)
    args = parser.parse_args()
    inputs = {k: getattr(args, k) for k in ("corpus", "frozen", "frozen_sha256", "policy", "policy_sha256",
                                          "development", "development_sha256", "composite_id", "manifest_sha256")}
    benchmark = load_benchmark(**inputs)
    require(args.smoke_cases_per_split is None or args.smoke_cases_per_split > 0, "Invalid smoke size")
    args.attempt.mkdir(mode=0o700)
    from scripts.lab_evaluator_runtime import evaluate_revision
    reports = {}
    failure = None
    for role in ("incumbent", "candidate"):
        try:
            reports[role] = evaluate_revision(attempt=args.attempt / role, repo=args.plugin_repo,
                revision=getattr(args, role), corpus=args.corpus, image=args.image,
                name=args.name + "-" + role, benchmark=benchmark, smoke=args.smoke_cases_per_split)
        except Exception as error:
            # No question, returned body or candidate-controlled error text on stdout.
            failure = {"role": role, "error_class": type(error).__name__}
            break
    result: dict[str, Any] = (verdict(reports["incumbent"], reports["candidate"]) if failure is None else
              {"score_eligible": False, "promoted": False, "independent_review_required": True,
               "reasons": ["execution_failed"], "failure": failure})
    if args.smoke_cases_per_split is not None:
        result["score_eligible"] = False
        result["reasons"].append("incomplete_smoke_not_authoritative")
    aggregate = {"protocol": PROTOCOL, "inputs": {k: str(v) for k, v in inputs.items()},
                 "reports": reports, "verdict": result}
    private_report(args.attempt / "aggregate.json", aggregate)
    print(json.dumps({"protocol": PROTOCOL, "verdict": result, "reports": reports}, sort_keys=True))
    if failure is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
