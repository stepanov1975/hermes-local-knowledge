#!/usr/bin/env python3
"""Run one local-knowledge ref against private frozen evaluation inputs.

This runner is intentionally small and JSON-only.  The orchestrator supplies a
ref checkout as both the working directory and the leading (clean) PYTHONPATH.
The runner verifies import provenance before calling the selected ref's public
index API.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict, is_dataclass
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping, Sequence, cast

DEFAULT_API_MODULE = "hermes_local_knowledge.indexer"


class ModuleProvenanceError(RuntimeError):
    """Raised when the selected API did not come from the intended ref."""


class LookupPayloadError(TypeError):
    """Raised when a public lookup result is not a JSON-native value."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _module_path(module: Any, ref_root: Path) -> Path:
    raw_path = getattr(module, "__file__", None)
    if not raw_path:
        raise ModuleProvenanceError("selected API module has no source file")
    module_path = Path(str(raw_path)).resolve()
    intended_root = ref_root.resolve()
    if not _is_within(module_path, intended_root):
        raise ModuleProvenanceError("selected API module resolved outside the intended ref")
    return module_path


def _import_api(name: str, ref_root: Path) -> tuple[Any, Path]:
    module = importlib.import_module(name)
    module_path = _module_path(module, ref_root)
    for required in ("build_index", "search_index", "get_artifact", "get_neighbors"):
        if not callable(getattr(module, required, None)):
            raise AttributeError(f"selected API module lacks callable {required}")
    return module, module_path


def _plumbing_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _plumbing_jsonable(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {str(key): _plumbing_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plumbing_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _lookup_json_value(value: Any) -> Any:
    value_type = type(value)
    if value is None or value_type in (bool, int, str):
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise LookupPayloadError("public lookup payload contains a non-finite float")
        return value
    if value_type is list:
        return [_lookup_json_value(item) for item in value]
    if value_type is dict:
        if any(type(key) is not str for key in value):
            raise LookupPayloadError("public lookup payload contains a non-string dictionary key")
        return {key: _lookup_json_value(item) for key, item in value.items()}
    raise LookupPayloadError("public lookup payload contains a non-JSON-native value")


def _safe_call(callback: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = _lookup_json_value(callback())
    except Exception as exc:  # individual replay failures are evaluator data
        rendered = f"{type(exc).__name__}: {exc}"
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_sha256": _sha256_text(rendered),
        }
    return {"status": "ok", "value": value}


def _settings_for(module: Any, raw: Any) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("settings must be an object or null")
    settings_type = getattr(module, "IndexSettings", None)
    if not callable(settings_type):
        raise AttributeError("selected API module lacks IndexSettings")
    values: dict[str, Any] = {}
    for key, value in raw.items():
        values[str(key)] = tuple(value) if isinstance(value, list) else value
    return settings_type(**values)


def _config_resolver(package_name: str, ref_root: Path) -> Callable[[Path], Any]:
    candidates = (
        (f"{package_name}.config", "resolve_config"),
        (f"{package_name}.runtime", "_runtime_config"),
    )
    for module_name, attribute in candidates:
        try:
            config_module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            continue
        _module_path(config_module, ref_root)
        resolver = getattr(config_module, attribute, None)
        if callable(resolver):
            return cast(Callable[[Path], Any], resolver)
    raise AttributeError("selected ref has no supported configuration resolver")


def _action_resolve_config(module: Any, request: dict[str, Any], ref_root: Path) -> dict[str, Any]:
    package_name = str(module.__name__).split(".", 1)[0]
    resolver = _config_resolver(package_name, ref_root)
    hermes_home = Path(str(request["hermes_home"]))
    config = resolver(hermes_home)
    settings = (
        asdict(cast(Any, config.index_settings))
        if is_dataclass(config.index_settings)
        else dict(config.index_settings)
    )
    root_override = request.get("root_override")
    source_root = Path(str(root_override)).expanduser().resolve() if root_override else config.source_root.resolve()
    if root_override and getattr(config, "include_markdown_docs_source", "default") == "default":
        settings["include_markdown_docs"] = True
    return {
        "source_root": str(source_root),
        "state_dir": str(config.state_dir.resolve()),
        "hermes_home": str(config.hermes_home.resolve()),
        "settings": _plumbing_jsonable(settings),
        "source_root_source": "cli" if root_override else str(config.source_root_source),
        "state_dir_source": str(config.state_dir_source),
        "include_markdown_docs_source": (
            "cli" if root_override and getattr(config, "include_markdown_docs_source", "default") == "default" else str(config.include_markdown_docs_source)
        ),
    }


def _action_materialize_fixture(request: dict[str, Any], ref_root: Path) -> dict[str, Any]:
    test_file = Path(str(request["test_file"])).resolve()
    if not _is_within(test_file, ref_root.resolve()):
        raise ModuleProvenanceError("fixture builder resolved outside the intended ref")
    destination = Path(str(request["destination"])).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location("_historical_baseline_test_indexer", test_file)
    if spec is None or spec.loader is None:
        raise ImportError("could not load pinned fixture builder")
    test_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = test_module
    spec.loader.exec_module(test_module)
    builder = getattr(test_module, "build_fixture", None)
    if not callable(builder):
        raise AttributeError("pinned tests/test_indexer.py has no build_fixture")
    source_root, hermes_home = builder(destination)
    return {
        "source_root": str(Path(source_root).resolve()),
        "hermes_home": str(Path(hermes_home).resolve()),
    }


def _action_build(module: Any, request: dict[str, Any]) -> dict[str, Any]:
    raw_builds = request.get("builds")
    if not isinstance(raw_builds, list):
        raise TypeError("builds must be a list")
    output: dict[str, Any] = {}
    for raw_build in raw_builds:
        if not isinstance(raw_build, dict):
            raise TypeError("each build must be an object")
        name = str(raw_build["name"])
        root = Path(str(raw_build["root"])).resolve()
        state_dir = Path(str(raw_build["state_dir"])).resolve()
        hermes_home = Path(str(raw_build["hermes_home"])).resolve()
        home = Path(str(raw_build.get("home") or hermes_home.parent)).resolve()
        os.environ["HOME"] = str(home)
        os.environ["HERMES_HOME"] = str(hermes_home)
        os.environ["LOCAL_KNOWLEDGE_ROOT"] = str(root)
        os.environ["LOCAL_KNOWLEDGE_STATE_DIR"] = str(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        settings = _settings_for(module, raw_build.get("settings"))
        started = time.perf_counter()
        if settings is None:
            artifacts, edges = module.build_index(root, state_dir, hermes_home)
        else:
            artifacts, edges = module.build_index(root, state_dir, hermes_home, settings)
        duration_ms = (time.perf_counter() - started) * 1000.0
        output[name] = {
            "artifact_count": len(artifacts),
            "edge_count": len(edges),
            "duration_ms": round(duration_ms, 3),
        }
    return {"builds": output}


def _search(module: Any, db_path: Path, query: str, limit: int | None, artifact_type: str | None) -> dict[str, Any]:
    def call() -> list[str]:
        kwargs: dict[str, Any] = {}
        if limit is not None:
            kwargs["limit"] = limit
        if artifact_type:
            kwargs["artifact_type"] = artifact_type
        rows = _lookup_json_value(module.search_index(db_path, query, **kwargs))
        if type(rows) is not list:
            raise TypeError("search_index did not return a list")
        ids: list[str] = []
        for row in rows:
            if type(row) is not dict or "id" not in row:
                raise TypeError("search_index returned a row without an id")
            artifact_id = row["id"]
            if type(artifact_id) is not str:
                raise LookupPayloadError("search_index returned a non-string artifact id")
            ids.append(artifact_id)
        return ids

    started = time.perf_counter()
    outcome = _safe_call(call)
    outcome["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    if outcome["status"] == "ok":
        outcome["ids"] = outcome.pop("value")
    return outcome


def _get(module: Any, db_path: Path, artifact_id: str) -> dict[str, Any]:
    outcome = _safe_call(lambda: module.get_artifact(db_path, artifact_id))
    if outcome["status"] == "ok":
        outcome["payload"] = outcome.pop("value")
    return outcome


def _neighbors(module: Any, db_path: Path, artifact_id: str, limit: int | None) -> dict[str, Any]:
    def call() -> Any:
        function = module.get_neighbors
        parameters = inspect.signature(function).parameters
        if limit is not None and "limit" in parameters:
            rows = function(db_path, artifact_id, limit=limit)
        else:
            rows = function(db_path, artifact_id)
        rows = _lookup_json_value(rows)
        if type(rows) is not list:
            raise TypeError("get_neighbors did not return a list")
        if limit is not None and "limit" not in parameters:
            rows = rows[:limit]
        return rows

    outcome = _safe_call(call)
    if outcome["status"] == "ok":
        outcome["payload"] = outcome.pop("value")
    return outcome


def _load_cases(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("frozen case file must contain an object")
    return payload


def _case_query_rows(cases: dict[str, Any]) -> list[dict[str, Any]]:
    labels = cases.get("labels", {})
    if not isinstance(labels, dict):
        raise TypeError("labels must be an object")
    rows: dict[str, dict[str, Any]] = {}
    for group_name in ("positive", "negative"):
        group = labels.get(group_name, [])
        if not isinstance(group, list):
            raise TypeError(f"labels.{group_name} must be a list")
        for row in group:
            if not isinstance(row, dict):
                raise TypeError("label case must be an object")
            rows[str(row["query_id"])] = row
    return list(rows.values())


def _truncate_search_outcome(outcome: dict[str, Any], limit: int | None) -> dict[str, Any]:
    if outcome.get("status") != "ok":
        return outcome
    comparison_limit = min(10, 10 if limit is None else max(0, limit))
    output = dict(outcome)
    output["ids"] = list(outcome.get("ids") or [])[:comparison_limit]
    return output


def _action_evaluate(module: Any, request: dict[str, Any]) -> dict[str, Any]:
    cases = _load_cases(Path(str(request["case_file"])))
    full_db = Path(str(request["full_db"])).resolve()
    synthetic_db = Path(str(request["synthetic_db"])).resolve()

    label_results: dict[str, Any] = {}
    for row in _case_query_rows(cases):
        label_results[str(row["query_id"])] = _search(module, full_db, str(row["query"]), 10, None)

    replay = cases.get("replay", {})
    if not isinstance(replay, dict):
        raise TypeError("replay must be an object")
    replay_results: dict[str, dict[str, Any]] = {"search": {}, "get": {}, "neighbors": {}}
    for row in replay.get("search", []):
        case_id = str(row["case_id"])
        recorded_limit = None if row.get("limit") is None else int(row["limit"])
        outcome = _search(
            module,
            full_db,
            str(row["query"]),
            recorded_limit,
            str(row.get("artifact_type") or "") or None,
        )
        replay_results["search"][case_id] = _truncate_search_outcome(outcome, recorded_limit)
    for row in replay.get("get", []):
        case_id = str(row["case_id"])
        replay_results["get"][case_id] = _get(module, full_db, str(row["artifact_id"]))
    for row in replay.get("neighbors", []):
        case_id = str(row["case_id"])
        replay_results["neighbors"][case_id] = _neighbors(
            module,
            full_db,
            str(row["artifact_id"]),
            None if row.get("limit") is None else int(row["limit"]),
        )

    synthetic_results: dict[str, Any] = {}
    synthetic = cases.get("synthetic", [])
    if not isinstance(synthetic, list):
        raise TypeError("synthetic must be a list")
    for row in synthetic:
        case_id = str(row["case_id"])
        synthetic_results[case_id] = _search(
            module,
            synthetic_db,
            str(row["query"]),
            int(row.get("limit", 10)),
            None,
        )

    return {
        "label_search": label_results,
        "replay": replay_results,
        "synthetic": synthetic_results,
    }


def _dispatch(module: Any, request: dict[str, Any], ref_root: Path) -> dict[str, Any]:
    action = str(request.get("action") or "")
    if action == "resolve_config":
        return _action_resolve_config(module, request, ref_root)
    if action == "materialize_fixture":
        return _action_materialize_fixture(request, ref_root)
    if action == "build":
        return _action_build(module, request)
    if action == "evaluate":
        return _action_evaluate(module, request)
    raise ValueError(f"unsupported evaluator action: {action}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True, help="private JSON request file")
    parser.add_argument("--ref-root", type=Path, required=True, help="intended ref checkout root")
    parser.add_argument("--api-module", default=DEFAULT_API_MODULE, help="explicit ref API module")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    captured_stdout = io.StringIO()
    status = 0
    try:
        with redirect_stdout(captured_stdout):
            request = json.loads(args.request.read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise TypeError("request must contain a JSON object")
            module, module_path = _import_api(str(args.api_module), args.ref_root)
            result = _dispatch(module, request, args.ref_root.resolve())
        payload: dict[str, Any] = {
            "ok": True,
            "api_module": str(args.api_module),
            "module_file": str(module_path),
            **result,
        }
    except Exception as exc:
        status = 1
        rendered = f"{type(exc).__name__}: {exc}"
        payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_sha256": _sha256_text(rendered),
        }
    captured = captured_stdout.getvalue()
    if captured:
        payload["captured_stdout_sha256"] = _sha256_text(captured)
        payload["captured_stdout_bytes"] = len(captured.encode("utf-8"))
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
