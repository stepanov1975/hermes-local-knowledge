#!/usr/bin/env python3
"""Build and exercise a compact, private human benchmark for local routing.

This module deliberately has one authoritative Python validation path. It does
not publish private cases and it never mutates an index or telemetry database.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
from typing import Any, Callable, Iterable, Iterator


JsonDict = dict[str, Any]
SearchFn = Callable[[str, str | None, int], list[str]]
HEX_CHARS = frozenset("0123456789abcdef")
VALID_RELEVANCE = {0, 1, 2, 3}
VALID_ROLES = {
    "canonical_owner",
    "required_support",
    "useful_alternative",
    "background",
    "irrelevant",
    "unknown",
}
VALID_LIFECYCLES = {"current", "historical", "draft", "plan", "retired", "unknown"}
VALID_ARTIFACT_TYPES = {
    "cron_job",
    "doc",
    "mcp_server",
    "memory_doc",
    "runbook",
    "script",
    "skill",
    "skill_support_doc",
    "tool_okf",
}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


class ValidationError(ValueError):
    """Raised when a benchmark artifact violates the compact contract."""


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> JsonDict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON {path}: {exc}") from exc
    return _dict(value, str(path))


def write_private_json(path: Path, value: object) -> None:
    resolved = path.expanduser().resolve(strict=False)
    if resolved.is_relative_to(REPOSITORY_ROOT):
        raise ValidationError(
            f"private output must be outside the public repository {REPOSITORY_ROOT}"
        )
    path = resolved
    try:
        path.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError:
        if not path.parent.is_dir():
            raise ValidationError(f"private output parent is not a directory: {path.parent}")
    else:
        os.chmod(path.parent, 0o700)
    if os.name != "nt" and stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise ValidationError(f"private output directory must have mode 0700: {path.parent}")
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def _frozen_index_snapshot(path: Path) -> Iterator[Path]:
    source_path = path.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="hermes-local-knowledge-index-") as directory:
        snapshot = Path(directory) / "index.sqlite"
        try:
            with source_path.open("rb") as source, snapshot.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        except OSError as exc:
            raise ValidationError(f"cannot snapshot frozen index {source_path}: {exc}") from exc
        yield snapshot


def _reject_output_alias(output: Path, inputs: Iterable[Path]) -> None:
    resolved_output = output.expanduser().resolve(strict=False)
    for input_path in inputs:
        resolved_input = input_path.expanduser().resolve(strict=False)
        same_path = resolved_output == resolved_input
        same_file = (
            resolved_output.exists()
            and resolved_input.exists()
            and resolved_output.samefile(resolved_input)
        )
        if same_path or same_file:
            raise ValidationError(f"output must not alias evaluator input {resolved_input}")


def _dict(value: object, label: str) -> JsonDict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValidationError(f"{label} must be an object with string keys")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return value


def _str(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"{label} must be a {'string' if allow_empty else 'nonempty string'}")
    return value


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} must be a boolean")
    return value


def _sha(value: object, label: str) -> str:
    text = _str(value, label)
    if len(text) != 64 or any(char not in HEX_CHARS for char in text):
        raise ValidationError(f"{label} must be a lowercase SHA-256")
    return text


def _unique_map(rows: object, key: str, label: str) -> dict[str, JsonDict]:
    output: dict[str, JsonDict] = {}
    for index, raw in enumerate(_list(rows, label)):
        row = _dict(raw, f"{label}[{index}]")
        identity = _str(row.get(key), f"{label}[{index}].{key}")
        if identity in output:
            raise ValidationError(f"duplicate {key} {identity!r} in {label}")
        output[identity] = row
    return output


def _normalize_none_needed(value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if value == "yes":
        return True
    if value == "no":
        return False
    raise ValidationError(f"{label} must be a boolean or 'yes'/'no'")


def _packet_cases(packet: JsonDict) -> dict[str, JsonDict]:
    if packet.get("schema_version") != 1:
        raise ValidationError("packet schema_version must be 1")
    _str(packet.get("packet_id"), "packet.packet_id")
    _sha(packet.get("rubric_sha256"), "packet.rubric_sha256")
    cases = _unique_map(packet.get("cases"), "case_id", "packet.cases")
    if not cases:
        raise ValidationError("packet must contain cases")
    for case_id, case in cases.items():
        _str(case.get("user_request"), f"packet case {case_id}.user_request")
        context = _list(case.get("preceding_context", []), f"packet case {case_id}.preceding_context")
        for context_index, raw_context in enumerate(context):
            context_item = _dict(raw_context, f"packet case {case_id}.preceding_context[{context_index}]")
            if set(context_item) != {"role", "content"}:
                raise ValidationError(
                    f"packet case {case_id}.preceding_context[{context_index}] has invalid fields"
                )
            if context_item.get("role") not in {"user", "assistant"}:
                raise ValidationError(
                    f"packet case {case_id}.preceding_context[{context_index}].role is invalid"
                )
            _str(
                context_item.get("content"),
                f"packet case {case_id}.preceding_context[{context_index}].content",
            )
        _str(case.get("search_query"), f"packet case {case_id}.search_query")
        artifact_type = case.get("artifact_type")
        if artifact_type is not None and (
            not isinstance(artifact_type, str) or artifact_type not in VALID_ARTIFACT_TYPES
        ):
            raise ValidationError(f"packet case {case_id}.artifact_type is invalid")
        _bool(case.get("high_impact"), f"packet case {case_id}.high_impact")
        items = _unique_map(case.get("items"), "item_id", f"packet case {case_id}.items")
        if not items:
            raise ValidationError(f"packet case {case_id} must contain items")
        for item_id, item in items.items():
            _str(item.get("artifact_type"), f"packet item {item_id}.artifact_type")
            _str(item.get("title"), f"packet item {item_id}.title")
            _str(item.get("summary"), f"packet item {item_id}.summary", allow_empty=True)
            _str(item.get("source_locator"), f"packet item {item_id}.source_locator")
    return cases


def _mapping_cases(
    mapping: JsonDict,
    packet: JsonDict,
    packet_cases: dict[str, JsonDict],
    index_sha256: str,
) -> dict[str, JsonDict]:
    if mapping.get("schema_version") != 1:
        raise ValidationError("mapping schema_version must be 1")
    if mapping.get("packet_id") != packet.get("packet_id"):
        raise ValidationError("mapping packet_id does not match packet")
    if _sha(mapping.get("packet_sha256"), "mapping.packet_sha256") != canonical_sha256(packet):
        raise ValidationError("mapping packet hash does not match packet")
    input_hashes = _dict(mapping.get("input_hashes"), "mapping.input_hashes")
    if _sha(input_hashes.get("index_sqlite"), "mapping index hash") != index_sha256:
        raise ValidationError("mapping index hash does not match frozen index")
    raw_cases = _dict(mapping.get("cases"), "mapping.cases")
    if set(raw_cases) != set(packet_cases):
        raise ValidationError("mapping case coverage does not match packet")
    output: dict[str, JsonDict] = {}
    for case_id, packet_case in packet_cases.items():
        mapped_case = _dict(raw_cases[case_id], f"mapping case {case_id}")
        mapped_items = _dict(mapped_case.get("items"), f"mapping case {case_id}.items")
        packet_item_ids = set(_unique_map(packet_case.get("items"), "item_id", "packet items"))
        if set(mapped_items) != packet_item_ids:
            raise ValidationError(f"mapping item coverage does not match packet for {case_id}")
        artifact_ids: set[str] = set()
        for item_id, raw in mapped_items.items():
            item = _dict(raw, f"mapping item {item_id}")
            artifact_id = _str(item.get("artifact_id"), f"mapping item {item_id}.artifact_id")
            if artifact_id in artifact_ids:
                raise ValidationError(
                    f"duplicate artifact_id {artifact_id!r} in mapping case {case_id}"
                )
            artifact_ids.add(artifact_id)
        output[case_id] = mapped_case
    return output


def _annotation_cases(
    annotation: JsonDict,
    packet: JsonDict,
    packet_cases: dict[str, JsonDict],
    label: str,
) -> dict[str, JsonDict]:
    if annotation.get("schema_version") != 1:
        raise ValidationError(f"{label} schema_version must be 1")
    _str(annotation.get("annotator"), f"{label}.annotator")
    if annotation.get("independent") is not True:
        raise ValidationError(f"{label} must be marked independent")
    if _sha(annotation.get("packet_sha256"), f"{label}.packet_sha256") != canonical_sha256(packet):
        raise ValidationError(f"{label} packet hash does not match packet")
    judgments = _unique_map(annotation.get("judgments"), "case_id", f"{label}.judgments")
    if set(judgments) != set(packet_cases):
        raise ValidationError(f"{label} case coverage does not match packet")
    for case_id, packet_case in packet_cases.items():
        judgment = judgments[case_id]
        _normalize_none_needed(judgment.get("none_needed"), f"{label} case {case_id}.none_needed")
        _str(judgment.get("case_rationale"), f"{label} case {case_id}.case_rationale")
        items = _unique_map(judgment.get("items"), "item_id", f"{label} case {case_id}.items")
        packet_items = _unique_map(packet_case.get("items"), "item_id", "packet items")
        if set(items) != set(packet_items):
            raise ValidationError(f"{label} item coverage does not match packet for {case_id}")
        for item_id, item in items.items():
            relevance = item.get("relevance")
            if not isinstance(relevance, int) or isinstance(relevance, bool) or relevance not in VALID_RELEVANCE:
                raise ValidationError(f"{label} item {item_id}.relevance must be 0..3")
            if item.get("role") not in VALID_ROLES:
                raise ValidationError(f"{label} item {item_id}.role is invalid")
            if item.get("lifecycle") not in VALID_LIFECYCLES:
                raise ValidationError(f"{label} item {item_id}.lifecycle is invalid")
            _bool(item.get("harmful_if_primary"), f"{label} item {item_id}.harmful_if_primary")
            _str(item.get("rationale"), f"{label} item {item_id}.rationale")
    return judgments


def prepare_review(
    packet: JsonDict,
    mapping: JsonDict,
    rater_a: JsonDict,
    rater_b: JsonDict,
    *,
    index_sha256: str,
) -> JsonDict:
    """Create a deterministic draft packet; no proposal is human gold."""

    index_hash = _sha(index_sha256, "index hash")
    packet_cases = _packet_cases(packet)
    _mapping_cases(mapping, packet, packet_cases, index_hash)
    if canonical_sha256(rater_a) == canonical_sha256(rater_b):
        raise ValidationError("rater_a and rater_b annotation inputs must be distinct")
    if _str(rater_a.get("annotator"), "rater_a.annotator") == _str(
        rater_b.get("annotator"), "rater_b.annotator"
    ):
        raise ValidationError("rater_a and rater_b annotators must be distinct")
    left = _annotation_cases(rater_a, packet, packet_cases, "rater_a")
    right = _annotation_cases(rater_b, packet, packet_cases, "rater_b")
    review_cases: list[JsonDict] = []
    for case_id, case in packet_cases.items():
        left_case = left[case_id]
        right_case = right[case_id]
        packet_items = _unique_map(case["items"], "item_id", f"packet case {case_id}.items")
        left_items = _unique_map(left_case["items"], "item_id", f"rater_a case {case_id}.items")
        right_items = _unique_map(right_case["items"], "item_id", f"rater_b case {case_id}.items")
        proposed_items: list[JsonDict] = []
        review_items: list[JsonDict] = []
        for item_id, item in packet_items.items():
            first = left_items[item_id]
            second = right_items[item_id]
            disagreements = [
                field
                for field in ("relevance", "role", "lifecycle", "harmful_if_primary")
                if first[field] != second[field]
            ]
            proposed_items.append(
                {
                    "item_id": item_id,
                    "relevance": max(first["relevance"], second["relevance"]),
                    "canonical_current": (
                        first["role"] == second["role"] == "canonical_owner"
                        and first["lifecycle"] == second["lifecycle"] == "current"
                    ),
                    "harmful_if_primary": bool(
                        first["harmful_if_primary"] or second["harmful_if_primary"]
                    ),
                    "disagreements": disagreements,
                }
            )
            review_items.append(
                {
                    "item": copy.deepcopy(item),
                    "draft_a": {
                        key: copy.deepcopy(first[key])
                        for key in (
                            "relevance",
                            "role",
                            "lifecycle",
                            "harmful_if_primary",
                            "rationale",
                        )
                    },
                    "draft_b": {
                        key: copy.deepcopy(second[key])
                        for key in (
                            "relevance",
                            "role",
                            "lifecycle",
                            "harmful_if_primary",
                            "rationale",
                        )
                    },
                }
            )
        left_none = _normalize_none_needed(left_case["none_needed"], "rater_a.none_needed")
        right_none = _normalize_none_needed(right_case["none_needed"], "rater_b.none_needed")
        review_cases.append(
            {
                "case_id": case_id,
                "user_request": case["user_request"],
                "preceding_context": case.get("preceding_context", ""),
                "search_query": case["search_query"],
                "artifact_type": case.get("artifact_type"),
                "high_impact": case["high_impact"],
                "items": review_items,
                "proposal": {
                    "none_needed": left_none if left_none == right_none else None,
                    "none_needed_disagreement": left_none != right_none,
                    "items": proposed_items,
                },
            }
        )
    return {
        "schema_version": 1,
        "review_id": f"lean-{packet['packet_id']}",
        "human_gold_created": False,
        "proposal_policy": {
            "relevance": "maximum of the two draft grades; human confirmation required",
            "canonical_current": "true only when both drafts say canonical_owner and current",
            "harmful_if_primary": "true when either draft marks harmful",
            "none_needed": "set only when both drafts agree",
        },
        "source": {
            "packet_id": packet["packet_id"],
            "packet_sha256": canonical_sha256(packet),
            "mapping_sha256": canonical_sha256(mapping),
            "rater_a_sha256": canonical_sha256(rater_a),
            "rater_b_sha256": canonical_sha256(rater_b),
            "index_sha256": index_hash,
            "rubric_sha256": packet["rubric_sha256"],
        },
        "cases": review_cases,
    }


def _validate_review(review: JsonDict) -> dict[str, JsonDict]:
    if review.get("schema_version") != 1 or review.get("human_gold_created") is not False:
        raise ValidationError("review must be an unresolved schema_version 1 packet")
    _str(review.get("review_id"), "review.review_id")
    source = _dict(review.get("source"), "review.source")
    for field in (
        "packet_sha256",
        "mapping_sha256",
        "rater_a_sha256",
        "rater_b_sha256",
        "index_sha256",
        "rubric_sha256",
    ):
        _sha(source.get(field), f"review.source.{field}")
    cases = _unique_map(review.get("cases"), "case_id", "review.cases")
    if not cases:
        raise ValidationError("review must contain cases")
    for case_id, case in cases.items():
        proposal = _dict(case.get("proposal"), f"review case {case_id}.proposal")
        none_needed = proposal.get("none_needed")
        if none_needed is not None:
            _bool(none_needed, f"review case {case_id}.proposal.none_needed")
        proposal_items = _unique_map(
            proposal.get("items"), "item_id", f"review case {case_id}.proposal.items"
        )
        review_items = _unique_map(
            [entry["item"] for entry in _list(case.get("items"), f"review case {case_id}.items")],
            "item_id",
            f"review case {case_id}.item cards",
        )
        if set(proposal_items) != set(review_items):
            raise ValidationError(f"review proposal item coverage mismatch for {case_id}")
        for item_id, item in proposal_items.items():
            relevance = item.get("relevance")
            if not isinstance(relevance, int) or isinstance(relevance, bool) or relevance not in VALID_RELEVANCE:
                raise ValidationError(f"review proposal relevance invalid for {item_id}")
            _bool(item.get("canonical_current"), f"review proposal {item_id}.canonical_current")
            _bool(item.get("harmful_if_primary"), f"review proposal {item_id}.harmful_if_primary")
    return cases


def finalize_benchmark(
    review: JsonDict,
    decisions: JsonDict,
    packet: JsonDict,
    mapping: JsonDict,
    annotation_a: JsonDict,
    annotation_b: JsonDict,
    *,
    index_sha256: str,
    valid_artifact_ids: set[str],
) -> JsonDict:
    """Reconstruct sources, then materialize exact human-approved decisions."""

    reconstructed_review = prepare_review(
        packet,
        mapping,
        annotation_a,
        annotation_b,
        index_sha256=index_sha256,
    )
    if canonical_sha256(reconstructed_review) != canonical_sha256(review):
        raise ValidationError("supplied review does not match reconstructed review from bound sources")
    review_cases = _validate_review(review)
    review_hash = canonical_sha256(review)
    if decisions.get("schema_version") != 1:
        raise ValidationError("decisions schema_version must be 1")
    if _sha(decisions.get("review_sha256"), "decisions.review_sha256") != review_hash:
        raise ValidationError("decisions review hash does not match review")
    reviewer = _str(decisions.get("reviewer"), "decisions.reviewer")
    decision_cases = _unique_map(decisions.get("cases"), "case_id", "decisions.cases")
    if set(decision_cases) != set(review_cases):
        raise ValidationError("decisions case coverage does not match review")
    source = _dict(review["source"], "review.source")
    index_hash = _sha(index_sha256, "index hash")
    if index_hash != source["index_sha256"]:
        raise ValidationError("frozen index hash does not match review")
    if canonical_sha256(mapping) != source["mapping_sha256"]:
        raise ValidationError("mapping hash does not match review")
    raw_mapping_cases = _dict(mapping.get("cases"), "mapping.cases")
    if set(raw_mapping_cases) != set(review_cases):
        raise ValidationError("mapping case coverage does not match review")

    finalized_cases: list[JsonDict] = []
    for case_id, review_case in review_cases.items():
        decision = decision_cases[case_id]
        status = decision.get("status")
        if status not in {"accepted_proposal", "edited"}:
            raise ValidationError(f"decision {case_id} status must resolve the case")
        human_rationale = _str(decision.get("rationale"), f"decision {case_id}.rationale")
        proposal = _dict(review_case["proposal"], f"review case {case_id}.proposal")
        none_needed = proposal.get("none_needed")
        raw_overrides = _list(decision.get("overrides"), f"decision {case_id}.overrides")
        if status == "accepted_proposal":
            if decision.get("none_needed") is not None or raw_overrides:
                raise ValidationError(f"accepted proposal {case_id} cannot carry overrides")
        else:
            explicit_none = decision.get("none_needed")
            if explicit_none is not None:
                none_needed = _bool(explicit_none, f"decision {case_id}.none_needed")
        if none_needed is None:
            raise ValidationError(f"decision {case_id} leaves none_needed unresolved")

        proposal_items = _unique_map(
            proposal.get("items"), "item_id", f"review case {case_id}.proposal.items"
        )
        overrides = _unique_map(raw_overrides, "item_id", f"decision {case_id}.overrides")
        if not set(overrides) <= set(proposal_items):
            raise ValidationError(f"decision {case_id} contains an unknown override item")
        resolved_items = copy.deepcopy(proposal_items)
        for item_id, override in overrides.items():
            allowed = {"item_id", "relevance", "canonical_current", "harmful_if_primary"}
            if set(override) - allowed or set(override) == {"item_id"}:
                raise ValidationError(f"decision override {item_id} has invalid fields")
            target = resolved_items[item_id]
            if "relevance" in override:
                relevance = override["relevance"]
                if (
                    not isinstance(relevance, int)
                    or isinstance(relevance, bool)
                    or relevance not in VALID_RELEVANCE
                ):
                    raise ValidationError(f"decision override {item_id}.relevance must be 0..3")
                target["relevance"] = relevance
            for field in ("canonical_current", "harmful_if_primary"):
                if field in override:
                    target[field] = _bool(override[field], f"decision override {item_id}.{field}")

        mapped_case = _dict(raw_mapping_cases[case_id], f"mapping case {case_id}")
        mapped_items = _dict(mapped_case.get("items"), f"mapping case {case_id}.items")
        if set(mapped_items) != set(resolved_items):
            raise ValidationError(f"mapping item coverage does not match review for {case_id}")
        output_items: list[JsonDict] = []
        for item_id in proposal_items:
            mapped_item = _dict(mapped_items[item_id], f"mapping item {item_id}")
            artifact_id = _str(mapped_item.get("artifact_id"), f"mapping item {item_id}.artifact_id")
            if artifact_id not in valid_artifact_ids:
                raise ValidationError(f"artifact {artifact_id!r} is not present in the frozen index")
            resolved = resolved_items[item_id]
            output_items.append(
                {
                    "item_id": item_id,
                    "artifact_id": artifact_id,
                    "relevance": resolved["relevance"],
                    "canonical_current": resolved["canonical_current"],
                    "harmful_if_primary": resolved["harmful_if_primary"],
                }
            )
        finalized_cases.append(
            {
                "case_id": case_id,
                "user_request": review_case["user_request"],
                "preceding_context": review_case.get("preceding_context", ""),
                "search_query": review_case["search_query"],
                "artifact_type": review_case.get("artifact_type"),
                "high_impact": review_case["high_impact"],
                "none_needed": none_needed,
                "human_status": status,
                "human_rationale": human_rationale,
                "items": output_items,
            }
        )
    return {
        "schema_version": 1,
        "benchmark_id": f"{review['review_id']}-human-v1",
        "human_gold_created": True,
        "reviewer": reviewer,
        "source": {
            **copy.deepcopy(source),
            "review_sha256": review_hash,
        },
        "cases": finalized_cases,
    }


def _benchmark_cases(benchmark: JsonDict) -> dict[str, JsonDict]:
    if benchmark.get("schema_version") != 1 or benchmark.get("human_gold_created") is not True:
        raise ValidationError("benchmark must be finalized human gold")
    _str(benchmark.get("benchmark_id"), "benchmark.benchmark_id")
    _str(benchmark.get("reviewer"), "benchmark.reviewer")
    source = _dict(benchmark.get("source"), "benchmark.source")
    _sha(source.get("index_sha256"), "benchmark.source.index_sha256")
    cases = _unique_map(benchmark.get("cases"), "case_id", "benchmark.cases")
    for case_id, case in cases.items():
        _str(case.get("search_query"), f"benchmark case {case_id}.search_query")
        artifact_type = case.get("artifact_type")
        if artifact_type is not None and (
            not isinstance(artifact_type, str) or artifact_type not in VALID_ARTIFACT_TYPES
        ):
            raise ValidationError(f"benchmark case {case_id}.artifact_type is invalid")
        _bool(case.get("none_needed"), f"benchmark case {case_id}.none_needed")
        items = _unique_map(case.get("items"), "item_id", f"benchmark case {case_id}.items")
        artifact_ids: set[str] = set()
        for item_id, item in items.items():
            artifact_id = _str(item.get("artifact_id"), f"benchmark item {item_id}.artifact_id")
            if artifact_id in artifact_ids:
                raise ValidationError(f"duplicate artifact_id {artifact_id!r} in benchmark case {case_id}")
            artifact_ids.add(artifact_id)
            relevance = item.get("relevance")
            if not isinstance(relevance, int) or isinstance(relevance, bool) or relevance not in VALID_RELEVANCE:
                raise ValidationError(f"benchmark item {item_id}.relevance must be 0..3")
            _bool(item.get("canonical_current"), f"benchmark item {item_id}.canonical_current")
            _bool(item.get("harmful_if_primary"), f"benchmark item {item_id}.harmful_if_primary")
    return cases


def replay_benchmark(benchmark: JsonDict, search: SearchFn, *, limit: int = 10) -> JsonDict:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 3:
        raise ValidationError("replay limit must be an integer of at least 3")
    cases = _benchmark_cases(benchmark)
    output: list[JsonDict] = []
    for case_id, case in cases.items():
        artifact_type = case.get("artifact_type")
        if artifact_type is not None and not isinstance(artifact_type, str):
            raise ValidationError(f"benchmark case {case_id}.artifact_type is invalid")
        ids = search(case["search_query"], artifact_type, limit)
        if not isinstance(ids, list) or not all(isinstance(value, str) and value for value in ids):
            raise ValidationError(f"search returned invalid IDs for {case_id}")
        if len(ids) != len(set(ids)):
            raise ValidationError(f"search returned duplicate IDs for {case_id}")
        output.append({"case_id": case_id, "ids": ids})
    return {
        "schema_version": 1,
        "benchmark_sha256": canonical_sha256(benchmark),
        "index_sha256": benchmark["source"]["index_sha256"],
        "limit": limit,
        "cases": output,
    }


def _supersession_edges(records: object, ranking: list[str]) -> list[tuple[str, str]]:
    present = set(ranking)
    edges: list[tuple[str, str]] = []
    for index, raw in enumerate(_list(records, "authority.records")):
        record = _dict(raw, f"authority.records[{index}]")
        source = _str(record.get("artifact_id"), f"authority.records[{index}].artifact_id")
        if source not in present:
            continue
        if record.get("policy_eligible") is not True or record.get("confidence") != "high":
            continue
        if record.get("status") not in {"historical", "plan", "retired"}:
            continue
        for rel_index, raw_relationship in enumerate(
            _list(record.get("relationships", []), f"authority record {source}.relationships")
        ):
            relationship = _dict(raw_relationship, f"authority relationship {source}[{rel_index}]")
            if relationship.get("kind") != "superseded_by":
                continue
            target = _str(
                relationship.get("target_artifact_id"),
                f"authority relationship {source}[{rel_index}].target_artifact_id",
            )
            if target in present and source != target:
                edges.append((source, target))
    return sorted(set(edges), key=lambda pair: (ranking.index(pair[0]), ranking.index(pair[1])))


def _reject_cycles(edges: Iterable[tuple[str, str]]) -> None:
    graph: dict[str, set[str]] = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValidationError("explicit supersession graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for target in graph.get(node, set()):
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def apply_explicit_supersession(
    ranking: list[str], authority_records: object
) -> tuple[list[str], list[JsonDict]]:
    """Stably place explicit current successors before superseded returned sources."""

    if not all(isinstance(value, str) and value for value in ranking) or len(ranking) != len(set(ranking)):
        raise ValidationError("ranking must contain unique nonempty artifact IDs")
    output = list(ranking)
    edges = _supersession_edges(authority_records, output)
    _reject_cycles(edges)
    changes: list[JsonDict] = []
    for _ in range(max(1, len(output) * len(output))):
        moved = False
        for source, target in edges:
            source_index = output.index(source)
            target_index = output.index(target)
            if target_index > source_index:
                output.pop(target_index)
                output.insert(source_index, target)
                changes.append({"source": source, "target": target})
                moved = True
        if not moved:
            break
    else:
        raise ValidationError("explicit supersession ordering did not converge")
    unique_changes: list[JsonDict] = []
    seen: set[tuple[str, str]] = set()
    for change in changes:
        key = (change["source"], change["target"])
        if key not in seen:
            seen.add(key)
            unique_changes.append(change)
    return output, unique_changes


def _ranking_cases(rankings: JsonDict, benchmark: JsonDict, label: str) -> dict[str, JsonDict]:
    if rankings.get("schema_version") != 1:
        raise ValidationError(f"{label} schema_version must be 1")
    if rankings.get("benchmark_sha256") != canonical_sha256(benchmark):
        raise ValidationError(f"{label} benchmark hash does not match")
    if rankings.get("index_sha256") != benchmark["source"]["index_sha256"]:
        raise ValidationError(f"{label} index hash does not match benchmark")
    limit = rankings.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 3:
        raise ValidationError(f"{label} limit must be an integer of at least 3")
    cases = _unique_map(rankings.get("cases"), "case_id", f"{label}.cases")
    benchmark_cases = _benchmark_cases(benchmark)
    if set(cases) != set(benchmark_cases):
        raise ValidationError(f"{label} case coverage does not match benchmark")
    for case_id, case in cases.items():
        ids = _list(case.get("ids"), f"{label} case {case_id}.ids")
        if not all(isinstance(value, str) and value for value in ids) or len(ids) != len(set(ids)):
            raise ValidationError(f"{label} case {case_id} must contain unique artifact IDs")
        if len(ids) > limit:
            raise ValidationError(f"{label} case {case_id} exceeds declared limit")
    return cases


def build_candidate_rankings(baseline: JsonDict, authority: JsonDict) -> JsonDict:
    records = authority.get("records")
    _list(records, "authority.records")
    output_cases: list[JsonDict] = []
    for raw in _list(baseline.get("cases"), "baseline.cases"):
        case = _dict(raw, "baseline case")
        case_id = _str(case.get("case_id"), "baseline case.case_id")
        ids = list(_list(case.get("ids"), f"baseline case {case_id}.ids"))
        candidate_ids, changes = apply_explicit_supersession(ids, records)
        output_cases.append({"case_id": case_id, "ids": candidate_ids, "changes": changes})
    return {
        "schema_version": 1,
        "benchmark_sha256": baseline.get("benchmark_sha256"),
        "index_sha256": baseline.get("index_sha256"),
        "limit": baseline.get("limit"),
        "candidate_policy": "explicit-high-confidence-superseded-by-v1",
        "authority_sha256": canonical_sha256(authority),
        "cases": output_cases,
    }


def _score(benchmark_cases: dict[str, JsonDict], ranking_cases: dict[str, JsonDict]) -> JsonDict:
    result: JsonDict = {
        "eligible_cases": 0,
        "none_needed_cases": 0,
        "none_needed_with_results": 0,
        "acceptable_hit_at_1": 0,
        "acceptable_hit_at_3": 0,
        "canonical_current_at_1": 0,
        "harmful_at_1": 0,
        "unjudged_at_1": 0,
        "graded_relevance_at_1_sum": 0,
    }
    for case_id, case in benchmark_cases.items():
        ids = ranking_cases[case_id]["ids"]
        labels = {item["artifact_id"]: item for item in case["items"]}
        if case["none_needed"]:
            result["none_needed_cases"] += 1
            if ids:
                result["none_needed_with_results"] += 1
            continue
        result["eligible_cases"] += 1
        top = ids[0] if ids else None
        if top is not None and top not in labels:
            result["unjudged_at_1"] += 1
        top_label = labels.get(top, {})
        relevance = top_label.get("relevance", 0)
        result["graded_relevance_at_1_sum"] += relevance
        if relevance >= 2:
            result["acceptable_hit_at_1"] += 1
        if any(labels.get(artifact_id, {}).get("relevance", 0) >= 2 for artifact_id in ids[:3]):
            result["acceptable_hit_at_3"] += 1
        if top_label.get("canonical_current") is True:
            result["canonical_current_at_1"] += 1
        if top_label.get("harmful_if_primary") is True:
            result["harmful_at_1"] += 1
    return result


def compare_rankings(benchmark: JsonDict, baseline: JsonDict, authority: JsonDict) -> JsonDict:
    """Generate the sole allowed candidate internally and compare it with the incumbent."""

    benchmark_cases = _benchmark_cases(benchmark)
    baseline_cases = _ranking_cases(baseline, benchmark, "baseline")
    candidate = build_candidate_rankings(baseline, authority)
    candidate_cases = _ranking_cases(candidate, benchmark, "candidate")
    changed: list[JsonDict] = []
    for case_id in benchmark_cases:
        baseline_ids = baseline_cases[case_id]["ids"]
        candidate_ids = candidate_cases[case_id]["ids"]
        if set(baseline_ids) != set(candidate_ids) or len(baseline_ids) != len(candidate_ids):
            raise ValidationError(f"candidate membership differs from baseline for {case_id}")
        baseline_top = baseline_ids[0] if baseline_ids else None
        candidate_top = candidate_ids[0] if candidate_ids else None
        if baseline_top != candidate_top:
            changed.append(
                {"case_id": case_id, "baseline": baseline_top, "candidate": candidate_top}
            )
    return {
        "schema_version": 1,
        "benchmark_sha256": canonical_sha256(benchmark),
        "baseline_sha256": canonical_sha256(baseline),
        "authority_sha256": canonical_sha256(authority),
        "candidate_sha256": canonical_sha256(candidate),
        "candidate_policy": candidate["candidate_policy"],
        "baseline": _score(benchmark_cases, baseline_cases),
        "candidate": _score(benchmark_cases, candidate_cases),
        "changed_top_1": changed,
        "manual_review_required": bool(changed),
        "automatic_release_verdict": None,
    }


def _index_artifact_ids(path: Path) -> set[str]:
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute("SELECT id FROM artifacts").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValidationError(f"cannot read frozen index: {exc}") from exc
    return {str(row[0]) for row in rows}


def _cli_prepare(args: argparse.Namespace) -> JsonDict:
    index = Path(args.index)
    output = Path(args.output)
    _reject_output_alias(
        output,
        (Path(args.packet), Path(args.mapping), Path(args.rater_a), Path(args.rater_b), index),
    )
    review = prepare_review(
        load_json(Path(args.packet)),
        load_json(Path(args.mapping)),
        load_json(Path(args.rater_a)),
        load_json(Path(args.rater_b)),
        index_sha256=file_sha256(index),
    )
    write_private_json(output, review)
    return {"output": str(Path(args.output)), "cases": len(review["cases"]), "human_gold": False}


def _cli_finalize(args: argparse.Namespace) -> JsonDict:
    index = Path(args.index)
    output = Path(args.output)
    _reject_output_alias(
        output,
        (
            Path(args.review),
            Path(args.decisions),
            Path(args.packet),
            Path(args.mapping),
            Path(args.rater_a),
            Path(args.rater_b),
            index,
        ),
    )
    with _frozen_index_snapshot(index) as snapshot:
        benchmark = finalize_benchmark(
            load_json(Path(args.review)),
            load_json(Path(args.decisions)),
            load_json(Path(args.packet)),
            load_json(Path(args.mapping)),
            load_json(Path(args.rater_a)),
            load_json(Path(args.rater_b)),
            index_sha256=file_sha256(snapshot),
            valid_artifact_ids=_index_artifact_ids(snapshot),
        )
    write_private_json(output, benchmark)
    return {"output": str(Path(args.output)), "cases": len(benchmark["cases"]), "human_gold": True}


def _cli_replay(args: argparse.Namespace) -> JsonDict:
    from hermes_local_knowledge import index as index_module

    expected_module = (REPOSITORY_ROOT / "hermes_local_knowledge" / "index.py").resolve()
    loaded_module = Path(index_module.__file__ or "").resolve()
    if loaded_module != expected_module:
        raise ValidationError(
            f"replay must load index code from reviewed checkout {expected_module}, got {loaded_module}"
        )

    benchmark = load_json(Path(args.benchmark))
    index = Path(args.index)
    output = Path(args.output)
    _reject_output_alias(output, (Path(args.benchmark), index))
    with _frozen_index_snapshot(index) as snapshot:
        if file_sha256(snapshot) != benchmark["source"]["index_sha256"]:
            raise ValidationError("frozen index hash does not match benchmark")

        def search(query: str, artifact_type: str | None, limit: int) -> list[str]:
            return [
                str(row["id"])
                for row in index_module.search_index(
                    snapshot, query, limit=limit, artifact_type=artifact_type
                )
            ]

        rankings = replay_benchmark(benchmark, search, limit=args.limit)
    write_private_json(output, rankings)
    return {"output": str(Path(args.output)), "cases": len(rankings["cases"])}


def _cli_compare(args: argparse.Namespace) -> JsonDict:
    output = Path(args.output)
    _reject_output_alias(
        output,
        (Path(args.benchmark), Path(args.baseline), Path(args.authority)),
    )
    report = compare_rankings(
        load_json(Path(args.benchmark)),
        load_json(Path(args.baseline)),
        load_json(Path(args.authority)),
    )
    write_private_json(output, report)
    return {"output": str(Path(args.output)), "changed_top_1": len(report["changed_top_1"])}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="build a human-review packet")
    for name in ("packet", "mapping", "rater-a", "rater-b", "index", "output"):
        prepare.add_argument(f"--{name}", required=True)
    prepare.set_defaults(handler=_cli_prepare)

    finalize = subparsers.add_parser("finalize", help="materialize human-approved gold")
    for name in (
        "review",
        "decisions",
        "packet",
        "mapping",
        "rater-a",
        "rater-b",
        "index",
        "output",
    ):
        finalize.add_argument(f"--{name}", required=True)
    finalize.set_defaults(handler=_cli_finalize)

    replay = subparsers.add_parser("replay", help="run the incumbent on the frozen index")
    for name in ("benchmark", "index", "output"):
        replay.add_argument(f"--{name}", required=True)
    replay.add_argument("--limit", type=int, default=10)
    replay.set_defaults(handler=_cli_replay)

    compare = subparsers.add_parser(
        "compare", help="generate and compare the explicit-supersession candidate"
    )
    for name in ("benchmark", "baseline", "authority", "output"):
        compare.add_argument(f"--{name}", required=True)
    compare.set_defaults(handler=_cli_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except ValidationError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
