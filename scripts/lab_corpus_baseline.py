"""Trusted full-corpus baseline adapter, supplied separately read-only.

Unlike ticket 65's pilot, this performs no feedback/network/model calls. Host
controller authority and inspection receipts stay outside candidate mounts.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corpus_integrity(corpus: Path) -> dict:
    manifest = json.loads((corpus / "manifest.json").read_text())
    actual: dict[str, object] = {}
    for directory, dirs, files in os.walk(corpus, followlinks=False):
        for name in dirs + files:
            p = Path(directory) / name
            rel = p.relative_to(corpus).as_posix()
            if rel == "manifest.json":
                continue
            if p.is_symlink():
                assert p.resolve(strict=True).is_relative_to(corpus.resolve()), "Link escape"
                actual[rel] = {"link": os.readlink(p)}
            elif p.is_file():
                actual[rel] = {"sha256": digest(p), "size": p.stat().st_size}
    assert actual == manifest["inputs"], "Corpus manifest drift"
    return manifest


def locator(display: str, source: Path, scanner: Path, state: Path) -> str:
    path, separator, fragment = display.partition("#")
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = source / p
    for prefix, anchor in (("seed", state), ("scanner", scanner), ("source", source)):
        if p.is_relative_to(anchor):
            return prefix + "/" + p.relative_to(anchor).as_posix() + (separator + fragment if separator else "")
    raise ValueError("Unmapped native artifact")


def native() -> None:
    home, output, corpus = Path(os.environ["HERMES_HOME"]), Path("/output"), Path("/corpus")
    assert os.getuid() == 10000 and sys.executable == "/opt/hermes/.venv/bin/python"
    initial = {n: (home / n).exists() for n in ("knowledge", "cron", "auth.json", ".env", "state.db", "memories", "sessions")}
    assert not any(initial.values()), "Execution home is not fresh"
    assert digest(corpus / "manifest.json") == os.environ["LAB_CORPUS_MANIFEST_SHA256"]
    manifest = corpus_integrity(corpus)
    mapping = json.loads((corpus / "mapping.json").read_text())
    assert len({a["logical_id"] for a in mapping}) == len(mapping)
    state = home / "knowledge"
    shutil.copytree(corpus / "seed/okfs", state / "okfs")
    seeded = {p.relative_to(state / "okfs").as_posix(): digest(p) for p in (state / "okfs").rglob("*.md")}
    # The sealed official interpreter intentionally has no optional PyYAML.
    # Load ONLY the pure-Python parser already pinned in the same immutable
    # image's test environment; do not change sys.path or replace Hermes.
    yaml_roots = list(Path("/opt/lab-tools/lib").glob("python*/site-packages/yaml"))
    assert len(yaml_roots) == 1
    yaml_spec = importlib.util.spec_from_file_location("yaml", yaml_roots[0] / "__init__.py",
                                                     submodule_search_locations=[str(yaml_roots[0])])
    assert yaml_spec is not None and yaml_spec.loader is not None
    yaml_module = importlib.util.module_from_spec(yaml_spec)
    sys.modules["yaml"] = yaml_module
    yaml_spec.loader.exec_module(yaml_module)
    assert yaml_module.__version__ == "6.0.3"
    from hermes_cli.plugins import get_plugin_manager  # type: ignore[import-untyped]
    get_plugin_manager().discover_and_load()
    from tools.registry import registry  # type: ignore[import-untyped]
    entries = [e for e in registry.get_all_entries() if e.name == "knowledge_search"]
    assert len(entries) == 1
    entry = entries[0]
    package = entry.handler.__module__.rsplit(".", 1)[0]
    handler_file = sys.modules[entry.handler.__module__].__file__
    assert handler_file is not None
    handler = Path(handler_file).resolve()
    assert handler.is_relative_to(home / "plugins/local_knowledge")
    cfg = importlib.import_module(package + ".config").resolve_config()
    assert cfg.source_root == corpus / "source" and cfg.hermes_home == corpus / "scanner" and cfg.state_dir == state
    assert not cfg.okf.enabled and not cfg.okf.auto_generate
    assert not cfg.implicit_feedback.enabled and cfg.verified_routing.mode == "off" and cfg.index_max_age_seconds == 0
    from dataclasses import asdict
    assert json.loads(json.dumps(asdict(cfg.index_settings))) == manifest["settings"]
    indexer = importlib.import_module(package + ".indexer")
    built, edges = indexer.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings, force=True)
    expected = {a["id"]: a for a in mapping}
    actual = {a.id: a for a in built}
    (output / "coverage.json").write_text(json.dumps({"missing": sorted(set(expected)-set(actual)),
        "extra": [{"id": a.id, "type": a.type, "path": a.path} for a in built if a.id not in expected],
        "counts": dict(Counter(a.type for a in built))}, indent=2) + "\n")
    assert set(expected) == set(actual), "Exhaustive native ID coverage differs"
    for identity, a in actual.items():
        m = expected[identity]
        assert (a.type, a.source, locator(a.path, cfg.source_root, cfg.hermes_home, state)) == (m["type"], m["source"], m["locator"]), "Relocated logical mapping differs"
        p = corpus / m["locator"].split("#", 1)[0]
        if a.type == "skill":
            p /= "SKILL.md"
        assert digest(p) == m["content_sha256"], "Mapped captured content differs"
    db, jsonl = state / "index.sqlite", state / "index.jsonl"
    owner = importlib.import_module(package + ".index")
    owner._validate_sqlite(db, set(expected), len(edges), jsonl_path=jsonl)
    rows = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert {r["id"] for r in rows} == set(expected) and len(rows) == len(expected)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        assert con.execute("pragma integrity_check").fetchall() == [("ok",)]
        db_rows = con.execute("select id,type,path from artifacts order by id").fetchall()
        assert {r[0] for r in db_rows} == set(expected)
        edge_count = con.execute("select count(*) from edges").fetchone()[0]
    from model_tools import handle_function_call  # type: ignore[import-untyped]
    dispatcher = sys.modules["model_tools"].__file__
    assert dispatcher is not None and Path(dispatcher).is_relative_to("/opt/hermes")
    # Fixed protocol: one unscored native warm-up, then one native call per
    # representative type. These are routing probes, NOT Luna benchmark labels.
    representatives: dict[str, Any] = {}
    for a in sorted(built, key=lambda a: a.id):
        representatives.setdefault(a.type, a)
    warm = next(iter(representatives.values()))
    response = json.loads(handle_function_call("knowledge_search", {"query": warm.title, "limit": 5, "rebuild": False}, task_id="lk-lab-baseline-warm"))
    assert not response.get("error")
    evidence = []
    for kind, a in sorted(representatives.items()):
        started = time.perf_counter()
        raw = handle_function_call("knowledge_search", {"query": a.title, "limit": 5, "rebuild": False}, task_id="lk-lab-baseline", tool_call_id="baseline-" + kind)
        elapsed = (time.perf_counter() - started) * 1000
        result = json.loads(raw)
        assert not result.get("error") and isinstance(result.get("results"), list)
        assert len(result["results"]) <= 5
        evidence.append({"type": kind, "query": a.title, "originating_id": a.id,
                         "originating_logical_id": expected[a.id]["logical_id"],
                         "response": result, "elapsed_ms": elapsed,
                         "origin_hit_at_5": a.id in {r["id"] for r in result["results"]}})
    assert seeded == {p.relative_to(state / "okfs").as_posix(): digest(p) for p in (state / "okfs").rglob("*.md")}
    # Official tool imports may create an EMPTY cron directory. The boundary is
    # no copied/active jobs, not absence of an incidental core directory.
    cron_files = list((home / "cron").glob("*"))
    assert not cron_files, "Runtime cron was populated"
    assert not (home / "auth.json").exists()
    execution_config = json.loads((home / "config.yaml").read_text())
    assert "mcp_servers" not in execution_config and "mcp" not in execution_config
    corpus_integrity(corpus)
    report = {"initial_state": initial, "uid": os.getuid(), "interpreter": sys.executable,
              "handler": str(handler), "dispatcher": sys.modules["model_tools"].__file__,
              "counts": dict(Counter(a.type for a in built)), "artifacts": len(built),
              "logical_mapping_count": len(expected), "edges": edge_count, "integrity": "ok",
              "jsonl_sha256": digest(jsonl), "corpus_manifest_sha256": digest(corpus / "manifest.json"),
              "seed_count": len(seeded), "searches": evidence,
              "protocol": "one-native-warmup-plus-one-limit5-per-type; no feedback/learning/model",
              "effective_config": {"scanner": str(cfg.hermes_home), "source": str(cfg.source_root),
                                   "state": str(state), "auto_generate": False, "implicit_feedback": False,
                                   "verified_routing": "off"}}
    (output / "native-baseline.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    (output / "index-identity.json").write_text(json.dumps(db_rows) + "\n")
    print(json.dumps({"status": "passed", "artifacts": len(built), "types": len(representatives)}))


def baseline_command(name: str, image: str, attempt: Path, corpus: Path, evaluator: Path) -> list[str]:
    from scripts.lab_runtime import worker_command, PROVIDERS
    command = worker_command(name, image, name + "-unused", attempt, corpus, {p: ["1.1.1.1"] for p in PROVIDERS})
    command[command.index("--network") + 1] = "none"
    command[command.index("--label") + 1] = "lk.lab=ticket-66"
    command[-1] = "/trusted/baseline.py"
    command += []
    command[command.index("--entrypoint"):command.index("--entrypoint")] = ["--env", "LAB_CORPUS_MANIFEST_SHA256=" + digest(corpus / "manifest.json")]
    command[command.index("--entrypoint"):command.index("--entrypoint")] = ["--mount", f"type=bind,src={evaluator.resolve()},dst=/trusted/baseline.py,readonly"]
    return command


def verify_baseline_spec(spec: dict, attempt: Path, corpus: Path, evaluator: Path) -> None:
    from scripts.lab_runtime import verify_worker_spec
    # Reuse #65's strict four-bind/resource check, but explicitly authorize the
    # additional ONE trusted read-only file and stronger offline namespace.
    checked = json.loads(json.dumps(spec))
    mount = [m for m in checked["Mounts"] if m["Destination"] == "/trusted/baseline.py"]
    assert len(mount) == 1 and mount[0]["Source"] == str(evaluator.resolve()) and not mount[0]["RW"] and mount[0]["Type"] == "bind"
    assert checked["HostConfig"]["NetworkMode"] == "none" and checked["Config"]["Labels"]["lk.lab"] == "ticket-66"
    assert checked["Config"]["Entrypoint"] == ["/opt/hermes/.venv/bin/python"] and checked["Config"]["Cmd"] == ["/trusted/baseline.py"]
    assert "LAB_CORPUS_MANIFEST_SHA256=" + digest(corpus / "manifest.json") in checked["Config"]["Env"]
    checked["Mounts"].remove(mount[0])
    checked["HostConfig"]["NetworkMode"] = "container:offline-audit"
    checked["Config"]["Labels"]["lk.lab"] = "ticket-65"
    verify_worker_spec(checked, attempt, corpus, "offline-audit")


def baseline(*, attempt: Path, repo: Path, revision: str, corpus: Path, image: str, name: str,
             manifest_sha256: str) -> dict:
    from scripts.lab_runtime import prepare_attempt, run, scoped_name
    scoped_name(name)
    assert digest(corpus / "manifest.json") == manifest_sha256, "Pinned manifest drift"
    manifest = corpus_integrity(corpus)
    prepare_attempt(attempt, repo, revision)
    config_path = attempt / "home/config.yaml"
    cfg = json.loads(config_path.read_text())
    cfg["local_knowledge"].update(manifest["settings"])
    cfg["local_knowledge"].update({"source_root": "/corpus/source", "hermes_home": "/corpus/scanner"})
    config_path.write_text(json.dumps(cfg, sort_keys=True, indent=2) + "\n")
    evaluator = Path(__file__).resolve()
    # Fresh scoped exports only; no candidate code is executed by this setup.
    setup = ["docker", "run", "--rm", "--name", name + "-setup", "--label", "lk.lab=ticket-66",
             "--network", "none", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
             "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges:true", "--read-only",
             "--memory", "64m", "--cpus", "0.25", "--pids-limit", "16", "--entrypoint", "/bin/chown"]
    for child in ("home", "work", "output"):
        setup += ["--mount", f"type=bind,src={attempt / child},dst=/{child}"]
    run(setup + [image, "-R", "10000:10000", "/home", "/work", "/output"])
    image_spec = json.loads(run(["docker", "image", "inspect", image]))[0]
    (attempt / "image.json").write_text(json.dumps(image_spec, indent=2) + "\n")
    (attempt / "evaluator-identity.json").write_text(json.dumps({"sha256": digest(evaluator), "offline_justification": "trusted-model-free-native-baseline; no auth or network required"}) + "\n")
    worker = name + "-worker"
    run(baseline_command(worker, image, attempt, corpus, evaluator))
    try:
        before = json.loads(run(["docker", "inspect", worker]))[0]
        verify_baseline_spec(before, attempt, corpus, evaluator)
        (attempt / "worker-before.json").write_text(json.dumps(before, indent=2) + "\n")
        run(["docker", "start", worker])
        exit_code = run(["docker", "wait", worker], timeout=None).strip()
        after = json.loads(run(["docker", "inspect", worker]))[0]
        verify_baseline_spec(after, attempt, corpus, evaluator)
        (attempt / "worker-after.json").write_text(json.dumps(after, indent=2) + "\n")
        logs = subprocess.run(["docker", "logs", worker], capture_output=True, text=True,
                              env={k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}, check=True)
        (attempt / "worker.log").write_text(logs.stdout + logs.stderr)
        if exit_code != "0":
            raise RuntimeError(f"Native baseline failed; private evidence: {attempt}")
        report = json.loads((attempt / "output/native-baseline.json").read_text())
        corpus_integrity(corpus)
        assert report["corpus_manifest_sha256"] == digest(corpus / "manifest.json")
        return report
    finally:
        spec = json.loads(run(["docker", "inspect", worker]))[0]
        assert spec["Config"]["Labels"]["lk.lab"] == "ticket-66"
        run(["docker", "rm", "-f", "-v", worker])
        objects = json.loads(run(["docker", "ps", "-a", "--filter", "name=^/" + worker + "$", "--format", "json"]).strip() or "{}")
        assert not objects, "Worker remained after removal"
        (attempt / "cleanup.json").write_text(json.dumps({"worker_removed": True,
            "output_hashes": {p.name: digest(p) for p in (attempt / "output").iterdir() if p.is_file()}}) + "\n")


def main() -> None:
    if str(Path(__file__)) == "/trusted/baseline.py":
        native()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("attempt", "plugin-repo", "corpus"):
        parser.add_argument("--" + arg, type=Path, required=True)
    for arg in ("plugin-commit", "image", "name"):
        parser.add_argument("--" + arg, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    report = baseline(attempt=args.attempt.resolve(), repo=args.plugin_repo.resolve(), revision=args.plugin_commit,
                      corpus=args.corpus.resolve(), image=args.image, name=args.name,
                      manifest_sha256=args.manifest_sha256)
    print(json.dumps({"artifacts": report["artifacts"], "integrity": report["integrity"], "types": len(report["counts"])}))


if __name__ == "__main__":
    main()
