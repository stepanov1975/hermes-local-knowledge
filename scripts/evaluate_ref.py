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
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
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


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_sha256_file(path: Path) -> str | None:
    return _sha256_file(path) if path.is_file() else None


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
        raise AttributeError("selected fixture module has no build_fixture")
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
    groups: list[list[Any]] = []
    for group_name in ("positive", "negative"):
        group = labels.get(group_name, [])
        if not isinstance(group, list):
            raise TypeError(f"labels.{group_name} must be a list")
        groups.append(group)
    quality = labels.get("quality", {})
    if not isinstance(quality, dict):
        raise TypeError("labels.quality must be an object")
    for tier, group in quality.items():
        if not isinstance(group, list):
            raise TypeError(f"labels.quality.{tier} must be a list")
        groups.append(group)
    for group in groups:
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


def _persisted_feedback_bound(value: Any) -> int | None:
    if type(value) is not int or value < -1:
        return None
    return value


def _parsed_utc_timestamp(value: Any) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _bounded_persisted_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if type(value) is not int or not minimum <= value <= maximum:
        return None
    return value


def _persisted_limit(value: Any) -> int | None:
    return _bounded_persisted_int(value, minimum=1, maximum=2**31 - 1)


def _persisted_id(value: Any) -> int | None:
    return _bounded_persisted_int(value, minimum=1, maximum=2**63 - 1)


def _production_state_key(
    raw_bound: Any,
    implicit_feedback_max_id: Any = None,
    implicit_feedback_enabled: Any = None,
    implicit_min_confirmations: Any = None,
    implicit_max_generic_queries: Any = None,
    implicit_observed_at: Any = None,
) -> str:
    bound = _persisted_feedback_bound(raw_bound)
    if bound is None:
        return "unavailable"
    if bound == -1:
        return "explicit-legacy_implicit-unavailable"
    if implicit_feedback_enabled is False:
        return f"explicit-{bound}_implicit-disabled"
    implicit_bound = _bounded_persisted_int(
        implicit_feedback_max_id, minimum=0, maximum=2**63 - 1
    )
    min_confirmations = _bounded_persisted_int(
        implicit_min_confirmations, minimum=1, maximum=10
    )
    max_generic_queries = _bounded_persisted_int(
        implicit_max_generic_queries, minimum=1, maximum=100
    )
    if (
        implicit_bound is None
        or type(implicit_feedback_enabled) is not bool
        or min_confirmations is None
        or max_generic_queries is None
    ):
        return f"bound-{bound}"
    state_key = (
        f"explicit-{bound}_implicit-{implicit_bound}"
        f"_enabled-{int(implicit_feedback_enabled)}"
        f"_min-{min_confirmations}"
        f"_generic-{max_generic_queries}"
    )
    if implicit_bound > 0:
        observed_at = _parsed_utc_timestamp(implicit_observed_at)
        if observed_at is None:
            return f"bound-{bound}"
        observed_key = hashlib.sha256(observed_at.isoformat().encode()).hexdigest()[:16]
        state_key += f"_observed-{observed_key}"
    return state_key


def _feedback_bound_kind(raw_bound: Any) -> str:
    bound = _persisted_feedback_bound(raw_bound)
    if bound is None:
        return "unavailable"
    return "legacy" if bound == -1 else f"bound-{bound}"


def _routing_provenance(metadata: Mapping[str, Any], service_module: Any) -> dict[str, Any]:
    key = str(getattr(service_module, "ROUTING_TRACE_METADATA_KEY", "_local_knowledge_routing_trace"))
    trace = metadata.get(key)
    decision = getattr(trace, "decision", None)
    outcome = getattr(decision, "outcome", None)
    if hasattr(outcome, "value"):
        outcome = getattr(outcome, "value")
    return {
        "baseline_ids": [str(value) for value in getattr(trace, "baseline_ids", ())],
        "route_outcome": str(outcome) if outcome is not None else None,
        "route_feedback_id": getattr(decision, "feedback_id", None),
        "route_artifact_id": getattr(decision, "artifact_id", None),
        "feedback_max_id": getattr(decision, "feedback_max_id", None),
        "implicit_feedback_max_id": getattr(decision, "implicit_feedback_max_id", None),
    }


def _production_services(
    module: Any,
    request: Mapping[str, Any],
    ref_root: Path,
) -> tuple[dict[str, Any], Any]:
    production = request.get("production")
    if not isinstance(production, dict):
        return {}, None
    raw_states = production.get("states")
    if not isinstance(raw_states, dict):
        raise TypeError("production.states must be an object")
    package_name = str(module.__name__).split(".", 1)[0]
    service_module = importlib.import_module(f"{package_name}.service")
    _module_path(service_module, ref_root)
    service_type = getattr(service_module, "LocalKnowledgeService", None)
    if not callable(service_type):
        raise AttributeError("selected ref has no LocalKnowledgeService")
    resolver = _config_resolver(package_name, ref_root)
    source_root = Path(str(production["source_root"])).resolve()
    hermes_home = Path(str(production["hermes_home"])).resolve()
    services: dict[str, Any] = {}
    raw_evidence = production.get("evidence")
    evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
    for state_key, raw_state_dir in raw_states.items():
        state_dir = Path(str(raw_state_dir)).resolve()
        os.environ["LOCAL_KNOWLEDGE_ROOT"] = str(source_root)
        os.environ["LOCAL_KNOWLEDGE_STATE_DIR"] = str(state_dir)
        os.environ["HERMES_HOME"] = str(hermes_home)
        config = resolver(hermes_home)
        if is_dataclass(config):
            config = replace(cast(Any, config), source_root=source_root, state_dir=state_dir)
            state_evidence = evidence.get(str(state_key), {})
            implicit = getattr(config, "implicit_feedback", None)
            if is_dataclass(implicit) and isinstance(state_evidence, dict):
                enabled = state_evidence.get("implicit_feedback_enabled")
                min_confirmations = state_evidence.get("implicit_min_confirmations")
                max_generic_queries = state_evidence.get("implicit_max_generic_queries")
                if enabled is False:
                    config = replace(
                        cast(Any, config),
                        implicit_feedback=replace(cast(Any, implicit), enabled=False),
                    )
                elif (
                    enabled is not None
                    and min_confirmations is not None
                    and max_generic_queries is not None
                ):
                    config = replace(
                        cast(Any, config),
                        implicit_feedback=replace(
                            cast(Any, implicit),
                            enabled=bool(enabled),
                            min_confirmations=int(min_confirmations),
                            max_generic_queries=int(max_generic_queries),
                        ),
                    )
        services[str(state_key)] = service_type(config)
    return services, service_module


def _production_search(
    service: Any,
    service_module: Any,
    row: Mapping[str, Any],
    *,
    feedback_bound_available: bool = False,
    implicit_feedback_bound_available: bool = False,
) -> dict[str, Any]:
    raw_recorded_limit = row.get("limit")
    recorded_limit = (
        None if raw_recorded_limit is None else _persisted_limit(raw_recorded_limit)
    )
    raw_route_feedback_id = row.get("route_feedback_id")
    recorded_route_feedback_id = (
        None if raw_route_feedback_id is None else _persisted_id(raw_route_feedback_id)
    )
    recorded_event_id = _persisted_id(row.get("event_id"))
    recorded_inputs_valid = (
        row.get("recorded_inputs_valid", True) is True
        and (raw_recorded_limit is None or recorded_limit is not None)
        and recorded_event_id is not None
        and (raw_route_feedback_id is None or recorded_route_feedback_id is not None)
    )
    companion_hash = _sha256_file(Path(str(service.config.state_dir)) / "index.jsonl")

    def call() -> dict[str, Any]:
        rows, metadata = service.search(
            str(row["query"]),
            limit=10 if recorded_limit is None else recorded_limit,
            artifact_type=str(row.get("artifact_type") or "") or None,
            rebuild=False,
            ensure=False,
        )
        rows = _lookup_json_value(rows)
        if type(rows) is not list:
            raise TypeError("LocalKnowledgeService.search did not return a list")
        ids: list[str] = []
        for result in rows:
            if type(result) is not dict or type(result.get("id")) is not str:
                raise LookupPayloadError("LocalKnowledgeService.search returned a row without a string id")
            ids.append(str(result["id"]))
        metadata = cast(Mapping[str, Any], metadata)
        recorded_hash = str(row.get("index_jsonl_sha256") or "") or None
        recorded_plugin_version = str(row.get("plugin_version") or "") or None
        replay_plugin_version = str(metadata.get("plugin_version") or "") or None
        recorded_format_version = str(row.get("index_format_version") or "") or None
        replay_format_version = str(metadata.get("format_version") or "") or None
        feedback_max_id = _persisted_feedback_bound(row.get("feedback_max_id", -1))
        implicit_feedback_max_id = _bounded_persisted_int(
            row.get("implicit_feedback_max_id"), minimum=0, maximum=2**63 - 1
        )
        raw_implicit_enabled = row.get("implicit_feedback_enabled")
        recorded_implicit_enabled = (
            raw_implicit_enabled if type(raw_implicit_enabled) is bool else None
        )
        implicit_min_confirmations = _bounded_persisted_int(
            row.get("implicit_min_confirmations"), minimum=1, maximum=10
        )
        implicit_max_generic_queries = _bounded_persisted_int(
            row.get("implicit_max_generic_queries"), minimum=1, maximum=100
        )
        recorded_implicit_config = (
            recorded_implicit_enabled,
            implicit_min_confirmations,
            implicit_max_generic_queries,
        )
        replay_implicit = getattr(service.config, "implicit_feedback", None)
        implicit_config_match = (
            replay_implicit is not None
            and recorded_implicit_enabled is not None
            and bool(recorded_implicit_enabled) == replay_implicit.enabled
            and (
                not replay_implicit.enabled
                or recorded_implicit_config
                == (
                    replay_implicit.enabled,
                    replay_implicit.min_confirmations,
                    replay_implicit.max_generic_queries,
                )
            )
        )
        corpus_match = None if recorded_hash is None else recorded_hash == companion_hash
        implicit_state_relevant = bool(recorded_implicit_enabled)
        implicit_state_exact = (
            implicit_feedback_bound_available and implicit_feedback_max_id is not None
            if implicit_state_relevant
            else recorded_implicit_enabled is not None
        )
        event_inputs_exact = (
            recorded_inputs_valid
            and feedback_bound_available
            and implicit_state_exact
            and feedback_max_id is not None
            and feedback_max_id >= 0
            and implicit_config_match
            and corpus_match is True
            and row.get("baseline_top_ids_valid") is True
            and row.get("recorded_top_ids_valid") is True
        )
        plugin_version_match = (
            None
            if recorded_plugin_version is None
            else recorded_plugin_version == replay_plugin_version
        )
        index_format_match = (
            None
            if recorded_format_version is None
            else recorded_format_version == replay_format_version
        )
        provenance = _routing_provenance(metadata, service_module)
        recorded_baseline_ids = [str(value) for value in row.get("baseline_top_ids", [])]
        recorded_final_ids = [str(value) for value in row.get("recorded_top_ids", [])]
        recorded_output_match = (
            None
            if not event_inputs_exact
            else (
                list(provenance.get("baseline_ids", [])) == recorded_baseline_ids
                and ids == recorded_final_ids
            )
        )
        recorded_route_match = (
            None
            if not event_inputs_exact
            else (
                str(provenance.get("route_outcome") or "none")
                == str(row.get("route_outcome") or "none")
                and provenance.get("route_feedback_id") == recorded_route_feedback_id
                and provenance.get("route_artifact_id") == row.get("route_artifact_id")
                and provenance.get("feedback_max_id") == feedback_max_id
                and (
                    not implicit_state_relevant
                    or provenance.get("implicit_feedback_max_id")
                    == implicit_feedback_max_id
                )
            )
        )
        return {
            "ids": ids,
            **provenance,
            "index_jsonl_sha256": companion_hash,
            "index_format_version": metadata.get("format_version"),
            "recorded_index_jsonl_sha256": recorded_hash,
            "corpus_match": corpus_match,
            "corpus_match_basis": "index_jsonl_sha256",
            "recorded_plugin_version": recorded_plugin_version,
            "replay_plugin_version": replay_plugin_version,
            "plugin_version_match": plugin_version_match,
            "recorded_index_format_version": recorded_format_version,
            "index_format_match": index_format_match,
            "event_inputs_exact": event_inputs_exact,
            "recorded_inputs_valid": recorded_inputs_valid,
            "implicit_config_match": implicit_config_match,
            "recorded_output_match": recorded_output_match,
            "recorded_route_match": recorded_route_match,
            "event_time_exact": (
                event_inputs_exact
                and plugin_version_match is True
                and index_format_match is True
                and recorded_output_match is True
                and recorded_route_match is True
            ),
            "feedback_bound_kind": _feedback_bound_kind(feedback_max_id),
        }

    started = time.perf_counter()
    outcome = _safe_call(call)
    outcome["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    if outcome["status"] == "ok":
        value = cast(dict[str, Any], outcome.pop("value"))
        outcome.update(value)
        comparison_limit = min(10, 10 if recorded_limit is None else max(0, recorded_limit))
        outcome["ids"] = list(outcome.get("ids") or [])[:comparison_limit]
    return outcome


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
    production_services, service_module = _production_services(
        module,
        request,
        Path(str(request.get("ref_root") or Path.cwd())).resolve(),
    )
    production_request = request.get("production", {})
    production_evidence = (
        production_request.get("evidence", {})
        if isinstance(production_request, dict)
        else {}
    )
    if not isinstance(production_evidence, dict):
        raise TypeError("production evidence must be an object")
    production_results: dict[str, Any] = {}
    usage_hashes_before = {
        key: _optional_sha256_file(Path(str(service.config.state_dir)) / "usage.sqlite")
        for key, service in production_services.items()
    }
    for row in replay.get("search", []):
        case_id = str(row["case_id"])
        raw_recorded_limit = row.get("limit")
        recorded_limit = (
            None if raw_recorded_limit is None else _persisted_limit(raw_recorded_limit)
        )
        outcome = _search(
            module,
            full_db,
            str(row["query"]),
            recorded_limit,
            str(row.get("artifact_type") or "") or None,
        )
        replay_results["search"][case_id] = _truncate_search_outcome(outcome, recorded_limit)
        if production_services:
            state_key = _production_state_key(
                row.get("feedback_max_id", -1),
                row.get("implicit_feedback_max_id"),
                row.get("implicit_feedback_enabled"),
                row.get("implicit_min_confirmations"),
                row.get("implicit_max_generic_queries"),
                row.get("observed_at"),
            )
            if state_key not in production_services:
                raise KeyError(f"missing production replay state: {state_key}")
            production_results[case_id] = _production_search(
                production_services[state_key],
                service_module,
                row,
                feedback_bound_available=bool(
                    production_evidence.get(state_key, {}).get(
                        "feedback_bound_available",
                        False,
                    )
                ),
                implicit_feedback_bound_available=bool(
                    production_evidence.get(state_key, {}).get(
                        "implicit_feedback_bound_available",
                        False,
                    )
                ),
            )
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
        "production_replay": production_results,
        "production_usage_hashes": {
            key: {
                "before": before,
                "after": _optional_sha256_file(
                    Path(str(production_services[key].config.state_dir)) / "usage.sqlite"
                ),
            }
            for key, before in usage_hashes_before.items()
        },
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
        request = dict(request)
        request["ref_root"] = str(ref_root)
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
