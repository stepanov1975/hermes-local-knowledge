from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace
import sys
from typing import Any

import pytest


def load_evaluator() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_ref.py"
    spec = importlib.util.spec_from_file_location("evaluate_ref_contract_tests", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class PrivateRecord:
    secret: str


class PrivateObject:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def __repr__(self) -> str:
        return self.secret

    def __str__(self) -> str:
        return self.secret


class FakeLookupModule:
    def __init__(self, *, search: Any, artifact: Any, neighbors: Any) -> None:
        self.search_result = search
        self.artifact_result = artifact
        self.neighbor_result = neighbors

    def search_index(self, _db_path: Path, _query: str, **_kwargs: Any) -> Any:
        return self.search_result

    def get_artifact(self, _db_path: Path, _artifact_id: str) -> Any:
        return self.artifact_result

    def get_neighbors(self, _db_path: Path, _artifact_id: str, limit: int | None = None) -> Any:
        del limit
        return self.neighbor_result


def test_case_query_rows_includes_quality_only_queries() -> None:
    evaluator = load_evaluator()

    rows = evaluator._case_query_rows(
        {
            "labels": {
                "positive": [
                    {"query_id": "aggregate", "query": "aggregate query", "accepted_ids": ["skill:aggregate"]}
                ],
                "negative": [],
                "quality": {
                    "explicit_resolution": [
                        {
                            "query_id": "quality-only",
                            "query": "correction trigger",
                            "accepted_ids": ["skill:target"],
                        }
                    ],
                    "verified_event": [],
                    "direct_or_legacy": [],
                },
            }
        }
    )

    assert {row["query_id"]: row["query"] for row in rows} == {
        "aggregate": "aggregate query",
        "quality-only": "correction trigger",
    }


def test_normal_lookup_results_accept_nested_json_values() -> None:
    evaluator = load_evaluator()
    artifact = {
        "id": "skill:alpha",
        "metadata": {
            "nullable": None,
            "booleans": [True, False],
            "numbers": [0, -2, 1.25],
            "text": "alpha",
            "nested": [{"value": "ok"}],
        },
    }
    neighbors = [{"id": "script:alpha", "edge": {"weight": 0.5, "active": True}}]
    module = FakeLookupModule(search=[artifact], artifact=artifact, neighbors=neighbors)
    db_path = Path("index.sqlite")

    search = evaluator._search(module, db_path, "alpha", 10, None)
    get = evaluator._get(module, db_path, "skill:alpha")
    neighbor_result = evaluator._neighbors(module, db_path, "skill:alpha", 5)

    assert search["status"] == "ok"
    assert search["ids"] == ["skill:alpha"]
    assert math.isfinite(search["duration_ms"])
    assert get == {"status": "ok", "payload": artifact}
    assert neighbor_result == {"status": "ok", "payload": neighbors}
    json.dumps({"search": search, "get": get, "neighbors": neighbor_result}, allow_nan=False)


@pytest.mark.parametrize(
    "invalid",
    [
        {"private-alpha"},
        ("private-alpha",),
        Path("private-alpha"),
        PrivateRecord("private-alpha"),
        PrivateObject("private-alpha"),
        {7: "private-alpha"},
        math.nan,
        math.inf,
        -math.inf,
    ],
    ids=["set", "tuple", "path", "dataclass", "custom", "non-string-key", "nan", "inf", "negative-inf"],
)
def test_get_rejects_non_json_public_values(invalid: Any) -> None:
    evaluator = load_evaluator()
    module = FakeLookupModule(search=[], artifact={"value": invalid}, neighbors=[])

    outcome = evaluator._get(module, Path("index.sqlite"), "skill:alpha")

    assert outcome["status"] == "error"
    assert outcome["error_type"] == "LookupPayloadError"
    assert set(outcome) == {"status", "error_type", "error_sha256"}
    assert "private-alpha" not in json.dumps(outcome, sort_keys=True)


@pytest.mark.parametrize("lookup", ["search", "get", "neighbors"])
def test_every_lookup_rejects_nested_non_json_values(lookup: str) -> None:
    evaluator = load_evaluator()
    invalid = {"id": "skill:alpha", "private": ("private-alpha",)}
    module = FakeLookupModule(search=[invalid], artifact=invalid, neighbors=[invalid])
    db_path = Path("index.sqlite")

    if lookup == "search":
        outcome = evaluator._search(module, db_path, "alpha", 10, None)
    elif lookup == "get":
        outcome = evaluator._get(module, db_path, "skill:alpha")
    else:
        outcome = evaluator._neighbors(module, db_path, "skill:alpha", 5)

    assert outcome["status"] == "error"
    assert outcome["error_type"] == "LookupPayloadError"
    assert "private-alpha" not in json.dumps(outcome, sort_keys=True)


def test_search_rejects_json_native_non_string_artifact_id() -> None:
    evaluator = load_evaluator()
    module = FakeLookupModule(search=[{"id": 7}], artifact=None, neighbors=[])

    outcome = evaluator._search(module, Path("index.sqlite"), "alpha", 10, None)

    assert outcome["status"] == "error"
    assert outcome["error_type"] == "LookupPayloadError"
    assert set(outcome) == {"status", "error_type", "error_sha256", "duration_ms"}


def test_lookup_payload_errors_are_deterministic_and_redacted() -> None:
    evaluator = load_evaluator()
    outcomes = []
    for secret in ("private-alpha", "private-beta"):
        module = FakeLookupModule(search=[], artifact={"value": PrivateObject(secret)}, neighbors=[])
        outcomes.append(evaluator._get(module, Path("index.sqlite"), "skill:alpha"))

    assert outcomes[0] == outcomes[1]
    rendered = json.dumps(outcomes, sort_keys=True)
    assert "private-alpha" not in rendered
    assert "private-beta" not in rendered


def test_main_emits_one_json_object_for_lookup_payload_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evaluator = load_evaluator()
    secret = "private-main-payload"
    cases = {
        "labels": {"positive": [], "negative": []},
        "replay": {
            "search": [],
            "get": [{"case_id": "invalid", "artifact_id": "skill:alpha"}],
            "neighbors": [],
        },
        "synthetic": [],
    }
    case_file = tmp_path / "cases.json"
    case_file.write_text(json.dumps(cases), encoding="utf-8")
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "action": "evaluate",
                "case_file": str(case_file),
                "full_db": str(tmp_path / "full.sqlite"),
                "synthetic_db": str(tmp_path / "synthetic.sqlite"),
            }
        ),
        encoding="utf-8",
    )
    module = FakeLookupModule(search=[], artifact=PrivateObject(secret), neighbors=[])
    monkeypatch.setattr(
        evaluator,
        "_import_api",
        lambda _name, _root: (module, tmp_path / "hermes_local_knowledge" / "indexer.py"),
    )

    status = evaluator.main(["--request", str(request_file), "--ref-root", str(tmp_path)])
    stdout = capsys.readouterr().out

    assert status == 0
    assert len(stdout.splitlines()) == 1
    payload = json.loads(stdout)
    error = payload["replay"]["get"]["invalid"]
    assert payload["ok"] is True
    assert error["status"] == "error"
    assert error["error_type"] == "LookupPayloadError"
    assert secret not in stdout


def test_production_search_is_read_only_and_emits_route_provenance(tmp_path: Path) -> None:
    evaluator = load_evaluator()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    index_jsonl = state_dir / "index.jsonl"
    index_jsonl.write_text('{"id":"skill:target"}\n', encoding="utf-8")
    usage_db = state_dir / "usage.sqlite"
    usage_db.write_bytes(b"immutable-usage")
    usage_before = usage_db.read_bytes()
    calls: list[dict[str, Any]] = []

    class FakeService:
        config = SimpleNamespace(state_dir=state_dir)

        def search(self, query: str, **kwargs: Any) -> tuple[list[dict[str, str]], dict[str, Any]]:
            calls.append({"query": query, **kwargs})
            decision = SimpleNamespace(
                outcome=SimpleNamespace(value="promoted_existing"),
                feedback_id=7,
                artifact_id="skill:target",
                feedback_max_id=9,
            )
            trace = SimpleNamespace(baseline_ids=("skill:other",), decision=decision)
            return [{"id": "skill:target"}], {
                "trace": trace,
                "format_version": "3",
                "plugin_version": "0.4.3",
            }

    service_module = SimpleNamespace(ROUTING_TRACE_METADATA_KEY="trace")
    row = {
        "query": "repair route",
        "limit": 5,
        "artifact_type": None,
        "feedback_max_id": 9,
        "index_jsonl_sha256": evaluator._sha256_file(index_jsonl),
        "index_format_version": "3",
        "plugin_version": "0.4.3",
        "baseline_top_ids": ["skill:other"],
        "recorded_top_ids": ["skill:target"],
        "route_outcome": "promoted_existing",
        "route_feedback_id": 7,
        "route_artifact_id": "skill:target",
    }
    output = evaluator._production_search(
        FakeService(), service_module, row, feedback_bound_available=True
    )

    assert calls == [
        {"query": "repair route", "limit": 5, "artifact_type": None, "rebuild": False, "ensure": False}
    ]
    assert usage_db.read_bytes() == usage_before
    assert output["ids"] == ["skill:target"]
    assert output["baseline_ids"] == ["skill:other"]
    assert output["route_outcome"] == "promoted_existing"
    assert output["route_feedback_id"] == 7
    assert output["route_artifact_id"] == "skill:target"
    assert output["feedback_max_id"] == 9
    assert output["event_inputs_exact"] is True
    assert output["plugin_version_match"] is True
    assert output["index_format_match"] is True
    assert output["recorded_output_match"] is True
    assert output["recorded_route_match"] is True
    assert output["event_time_exact"] is True

    mismatched = evaluator._production_search(
        FakeService(),
        service_module,
        {**row, "plugin_version": "0.4.2"},
        feedback_bound_available=True,
    )
    assert mismatched["event_inputs_exact"] is True
    assert mismatched["plugin_version_match"] is False
    assert mismatched["event_time_exact"] is False

    changed_output = evaluator._production_search(
        FakeService(),
        service_module,
        {**row, "recorded_top_ids": ["skill:different"]},
        feedback_bound_available=True,
    )
    assert changed_output["event_inputs_exact"] is True
    assert changed_output["recorded_output_match"] is False
    assert changed_output["event_time_exact"] is False
