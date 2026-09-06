#!/usr/bin/env python3
"""Prepare and finalize a small, private, explicitly human-labeled benchmark."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Iterator


JsonDict = dict[str, Any]
HEX_CHARS = frozenset("0123456789abcdef")
VALID_RELEVANCE = {0, 1, 2, 3}
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
REVIEWER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}")
REVIEW_ITEM_KEY_DOMAIN = b"hermes-local-knowledge/lean-review-item-key/v1\0"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


class ValidationError(ValueError):
    """Raised when a benchmark artifact violates the compact contract."""


def _require_private_filesystem_support() -> None:
    if os.name == "nt":
        raise ValidationError(
            "private benchmark files are unsupported on Windows because restrictive ACLs "
            "cannot be guaranteed"
        )


def _object(value: object, label: str) -> JsonDict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValidationError(f"{label} must be an object with string keys")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return value


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        kind = "string" if allow_empty else "nonempty string"
        raise ValidationError(f"{label} must be a {kind}")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label)
    if text != text.strip():
        raise ValidationError(f"{label} must not contain surrounding whitespace")
    return text


def _reviewer_identifier(value: object) -> str:
    text = _identifier(value, "review.reviewer")
    if REVIEWER_ID_PATTERN.fullmatch(text) is None:
        raise ValidationError(
            "review.reviewer must be a 1..64 character ASCII identifier using letters, digits, . _ @ or -"
        )
    return text


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} must be a boolean")
    return value


def _require_schema_version_one(value: object, label: str) -> None:
    if type(value) is not int or value != 1:
        raise ValidationError(f"{label} must be 1")


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(char not in HEX_CHARS for char in text):
        raise ValidationError(f"{label} must be a lowercase SHA-256")
    return text


def _unique_rows(rows: object, key: str, label: str) -> dict[str, JsonDict]:
    output: dict[str, JsonDict] = {}
    for index, raw_row in enumerate(_array(rows, label)):
        row = _object(raw_row, f"{label}[{index}]")
        identity = _identifier(row.get(key), f"{label}[{index}].{key}")
        if identity in output:
            raise ValidationError(f"duplicate {key} {identity!r} in {label}")
        output[identity] = row
    return output


def _require_exact_fields(value: JsonDict, expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValidationError(f"{label} has invalid fields")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"value must be JSON-serializable without non-finite numbers: {exc}") from exc
    return payload.encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _review_mapping_key(mapping: JsonDict) -> bytes:
    return hashlib.sha256(REVIEW_ITEM_KEY_DOMAIN + _canonical_json_bytes(mapping)).digest()


def _review_item_id(mapping_key: bytes, case_id: str, item_id: str) -> str:
    message = f"{case_id}\0{item_id}".encode("utf-8")
    digest = hmac.new(mapping_key, message, hashlib.sha256).hexdigest()
    return f"item-{digest[:32]}"


def _same_json(left: object, right: object) -> bool:
    return canonical_sha256(left) == canonical_sha256(right)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> JsonDict:
    output: JsonDict = {}
    for key, value in pairs:
        if key in output:
            raise ValidationError(f"duplicate JSON key {key!r}")
        output[key] = value
    return output


def _reject_nonfinite_json(value: str) -> None:
    raise ValidationError(f"non-finite JSON number {value!r} is not allowed")


def load_json(path: Path) -> JsonDict:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except ValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON {path}: {exc}") from exc
    return _object(value, str(path))


def _repository_roots() -> tuple[Path, ...]:
    roots = {REPOSITORY_ROOT}
    if not (REPOSITORY_ROOT / ".git").exists():
        return tuple(roots)
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        )
    except OSError as exc:
        raise ValidationError("cannot enumerate repository worktrees") from exc
    if result.returncode != 0:
        raise ValidationError("cannot enumerate repository worktrees")
    prefix = b"worktree "
    for field in result.stdout.split(b"\0"):
        if field.startswith(prefix):
            roots.add(Path(os.fsdecode(field.removeprefix(prefix))).resolve(strict=False))
    return tuple(sorted(roots, key=str))


def _resolved_private_path(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    for repository_root in _repository_roots():
        if resolved.is_relative_to(repository_root):
            raise ValidationError(
                f"{label} must be outside the public repository {repository_root}"
            )
    return resolved


def _validate_private_input(path: Path, label: str) -> Path:
    resolved = _resolved_private_path(path, label)
    try:
        file_mode = resolved.stat().st_mode
        parent_mode = resolved.parent.stat().st_mode
    except OSError as exc:
        raise ValidationError(f"cannot inspect private input {resolved}: {exc}") from exc
    if not stat.S_ISREG(file_mode):
        raise ValidationError(f"{label} must be a regular file: {resolved}")
    if stat.S_IMODE(file_mode) & 0o077:
        raise ValidationError(f"{label} must not grant group or other permissions: {resolved}")
    if not stat.S_ISDIR(parent_mode) or stat.S_IMODE(parent_mode) & 0o077:
        raise ValidationError(f"{label} parent directory must be owner-only: {resolved.parent}")
    return resolved


def _ensure_private_output_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while True:
        try:
            mode = current.stat().st_mode
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise ValidationError(f"cannot create private output directory: {path}")
            current = parent
            continue
        except OSError as exc:
            raise ValidationError(f"cannot inspect private output directory {current}: {exc}") from exc
        if not stat.S_ISDIR(mode):
            raise ValidationError(f"private output parent is not a directory: {current}")
        break

    for directory in reversed(missing):
        created = False
        try:
            directory.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValidationError(f"cannot create private output directory {directory}: {exc}") from exc
        if created:
            os.chmod(directory, 0o700)
        try:
            mode = directory.stat().st_mode
        except OSError as exc:
            raise ValidationError(f"cannot inspect private output directory {directory}: {exc}") from exc
        if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
            raise ValidationError(f"private output directory must have mode 0700: {directory}")

    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ValidationError(f"cannot inspect private output directory {path}: {exc}") from exc
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
        raise ValidationError(f"private output directory must have mode 0700: {path}")


def write_private_json(path: Path, value: object) -> None:
    _require_private_filesystem_support()
    resolved = _resolved_private_path(path, "private output")
    _ensure_private_output_directory(resolved.parent)
    try:
        payload = json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"private output must be JSON-serializable: {exc}") from exc

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{resolved.name}.", dir=resolved.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(resolved)
        os.chmod(resolved, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def _frozen_index_snapshot(path: Path) -> Iterator[Path]:
    _require_private_filesystem_support()
    source_path = _resolved_private_path(path, "frozen index")
    temporary_root = _resolved_private_path(
        Path(tempfile.gettempdir()),
        "temporary directory for private index snapshots",
    )
    with tempfile.TemporaryDirectory(
        prefix="hermes-local-knowledge-index-", dir=temporary_root
    ) as directory:
        snapshot_dir = Path(directory)
        snapshot_dir.chmod(0o700)
        snapshot = snapshot_dir / "index.sqlite"
        try:
            with source_path.open("rb") as source, snapshot.open("xb") as target:
                os.fchmod(target.fileno(), 0o600)
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
            raise ValidationError(f"output must not alias benchmark input {resolved_input}")


def _packet_cases(packet: JsonDict) -> dict[str, JsonDict]:
    _require_schema_version_one(packet.get("schema_version"), "packet.schema_version")
    _identifier(packet.get("packet_id"), "packet.packet_id")
    _sha256(packet.get("rubric_sha256"), "packet.rubric_sha256")
    _text(packet.get("instructions"), "packet.instructions")
    cases = _unique_rows(packet.get("cases"), "case_id", "packet.cases")
    if not cases:
        raise ValidationError("packet must contain cases")
    for case_id, case in cases.items():
        _text(case.get("user_request"), f"packet case {case_id}.user_request")
        context = _array(case.get("preceding_context", []), f"packet case {case_id}.preceding_context")
        for context_index, raw_context in enumerate(context):
            message = _object(
                raw_context,
                f"packet case {case_id}.preceding_context[{context_index}]",
            )
            _require_exact_fields(
                message,
                {"role", "content"},
                f"packet case {case_id}.preceding_context[{context_index}]",
            )
            if message.get("role") not in {"user", "assistant"}:
                raise ValidationError(
                    f"packet case {case_id}.preceding_context[{context_index}].role is invalid"
                )
            _text(
                message.get("content"),
                f"packet case {case_id}.preceding_context[{context_index}].content",
            )
        _text(case.get("search_query"), f"packet case {case_id}.search_query")
        artifact_type = case.get("artifact_type")
        if artifact_type is not None:
            artifact_type = _text(artifact_type, f"packet case {case_id}.artifact_type")
            if artifact_type not in VALID_ARTIFACT_TYPES:
                raise ValidationError(f"packet case {case_id}.artifact_type is unsupported")
        _boolean(case.get("high_impact"), f"packet case {case_id}.high_impact")
        items = _unique_rows(case.get("items"), "item_id", f"packet case {case_id}.items")
        if not items:
            raise ValidationError(f"packet case {case_id}.items must not be empty")
        for item_id, item in items.items():
            _require_exact_fields(
                item,
                {
                    "item_id",
                    "artifact_type",
                    "title",
                    "summary",
                    "source_locator",
                    "authority_evidence",
                },
                f"packet item {item_id}",
            )
            artifact_type = _text(
                item.get("artifact_type"), f"packet item {item_id}.artifact_type"
            )
            if artifact_type not in VALID_ARTIFACT_TYPES:
                raise ValidationError(f"packet item {item_id}.artifact_type is invalid")
            _text(item.get("title"), f"packet item {item_id}.title")
            _text(item.get("summary"), f"packet item {item_id}.summary", allow_empty=True)
            _text(item.get("source_locator"), f"packet item {item_id}.source_locator")
            evidence = _array(
                item.get("authority_evidence"),
                f"packet item {item_id}.authority_evidence",
            )
            for evidence_index, raw_evidence in enumerate(evidence):
                evidence_row = _object(
                    raw_evidence,
                    f"packet item {item_id}.authority_evidence[{evidence_index}]",
                )
                _require_exact_fields(
                    evidence_row,
                    {"citation", "excerpt"},
                    f"packet item {item_id}.authority_evidence[{evidence_index}]",
                )
                _text(
                    evidence_row.get("citation"),
                    f"packet item {item_id}.authority_evidence[{evidence_index}].citation",
                )
                _text(
                    evidence_row.get("excerpt"),
                    f"packet item {item_id}.authority_evidence[{evidence_index}].excerpt",
                )
    return cases


def _mapping_ids(
    mapping: JsonDict,
    packet: JsonDict,
    packet_cases: dict[str, JsonDict],
    *,
    index_sha256: str,
    valid_artifacts: dict[str, JsonDict],
) -> dict[str, dict[str, str]]:
    _require_schema_version_one(mapping.get("schema_version"), "mapping.schema_version")
    if mapping.get("packet_id") != packet.get("packet_id"):
        raise ValidationError("mapping packet_id does not match packet")
    if _sha256(mapping.get("packet_sha256"), "mapping.packet_sha256") != canonical_sha256(packet):
        raise ValidationError("mapping packet hash does not match packet")
    input_hashes = _object(mapping.get("input_hashes"), "mapping.input_hashes")
    if _sha256(input_hashes.get("index_sqlite"), "mapping index hash") != index_sha256:
        raise ValidationError("mapping index hash does not match frozen index")
    raw_cases = _object(mapping.get("cases"), "mapping.cases")
    if set(raw_cases) != set(packet_cases):
        raise ValidationError("mapping case coverage does not match packet")

    output: dict[str, dict[str, str]] = {}
    for case_id, packet_case in packet_cases.items():
        mapped_case = _object(raw_cases[case_id], f"mapping case {case_id}")
        mapped_items = _object(mapped_case.get("items"), f"mapping case {case_id}.items")
        packet_items = _unique_rows(
            packet_case.get("items"), "item_id", f"packet case {case_id}.items"
        )
        if set(mapped_items) != set(packet_items):
            raise ValidationError(f"mapping item coverage does not match packet for {case_id}")
        artifact_ids: set[str] = set()
        output[case_id] = {}
        for item_id, raw_mapping in mapped_items.items():
            mapping_item = _object(raw_mapping, f"mapping item {item_id}")
            artifact_id = _identifier(
                mapping_item.get("artifact_id"), f"mapping item {item_id}.artifact_id"
            )
            if artifact_id in artifact_ids:
                raise ValidationError(
                    f"duplicate artifact_id {artifact_id!r} in mapping case {case_id}"
                )
            artifact_ids.add(artifact_id)
            artifact = valid_artifacts.get(artifact_id)
            if artifact is None:
                raise ValidationError(f"artifact {artifact_id!r} is not present in the frozen index")
            packet_item = packet_items[item_id]
            for index_field, packet_field in (
                ("type", "artifact_type"),
                ("title", "title"),
                ("summary", "summary"),
                ("path", "source_locator"),
            ):
                if artifact.get(index_field) != packet_item.get(packet_field):
                    raise ValidationError(
                        f"mapping item {item_id} {index_field} does not match frozen index"
                    )
            output[case_id][item_id] = artifact_id
    return output


def _validated_inputs(
    packet: JsonDict,
    mapping: JsonDict,
    *,
    index_sha256: str,
    valid_artifacts: dict[str, JsonDict],
) -> tuple[str, dict[str, JsonDict], dict[str, dict[str, str]]]:
    index_hash = _sha256(index_sha256, "index hash")
    packet_cases = _packet_cases(packet)
    mapping_ids = _mapping_ids(
        mapping,
        packet,
        packet_cases,
        index_sha256=index_hash,
        valid_artifacts=valid_artifacts,
    )
    return index_hash, packet_cases, mapping_ids


def _review_source(packet: JsonDict, mapping: JsonDict, index_sha256: str) -> JsonDict:
    return {
        "packet_id": packet["packet_id"],
        "packet_sha256": canonical_sha256(packet),
        "mapping_sha256": canonical_sha256(mapping),
        "index_sha256": index_sha256,
        "rubric_sha256": packet["rubric_sha256"],
    }


def _review_template(
    packet: JsonDict,
    mapping: JsonDict,
    packet_cases: dict[str, JsonDict],
    *,
    index_sha256: str,
) -> JsonDict:
    cases: list[JsonDict] = []
    mapping_key = _review_mapping_key(mapping)
    for case_id, case in packet_cases.items():
        packet_items = _unique_rows(
            case.get("items"), "item_id", f"packet case {case_id}.items"
        )
        cases.append(
            {
                "case_id": case_id,
                "user_request": case["user_request"],
                "search_query": case["search_query"],
                "artifact_type": case.get("artifact_type"),
                "high_impact": case["high_impact"],
                "none_needed": None,
                "items": [
                    {
                        "item_id": _review_item_id(mapping_key, case_id, item_id),
                        "item_number": item_number,
                        "relevance": None,
                        "canonical_current": None,
                        "harmful_if_primary": None,
                    }
                    for item_number, item_id in enumerate(packet_items, start=1)
                ],
            }
        )
    return {
        "schema_version": 1,
        "review_id": f"lean-{packet['packet_id']}",
        "human_gold_created": False,
        "reviewer": None,
        "instructions": packet["instructions"],
        "source": _review_source(packet, mapping, index_sha256),
        "cases": cases,
    }


def prepare_review(
    packet: JsonDict,
    mapping: JsonDict,
    *,
    index_sha256: str,
    valid_artifacts: dict[str, JsonDict],
) -> JsonDict:
    """Build a blinded template whose labels must all be supplied by a human."""

    index_hash, packet_cases, _mapping = _validated_inputs(
        packet,
        mapping,
        index_sha256=index_sha256,
        valid_artifacts=valid_artifacts,
    )
    return _review_template(packet, mapping, packet_cases, index_sha256=index_hash)


def _validate_labeled_review(
    review: JsonDict,
    template: JsonDict,
) -> tuple[str, dict[str, JsonDict]]:
    _require_exact_fields(
        review,
        {
            "schema_version",
            "review_id",
            "human_gold_created",
            "reviewer",
            "instructions",
            "source",
            "cases",
        },
        "review",
    )
    _require_schema_version_one(review.get("schema_version"), "review.schema_version")
    if review.get("human_gold_created") is not False:
        raise ValidationError("review must be an unresolved schema_version 1 packet")
    if review.get("review_id") != template["review_id"]:
        raise ValidationError("review identity does not match reconstructed template")
    reviewer = _reviewer_identifier(review.get("reviewer"))
    if review.get("instructions") != template["instructions"]:
        raise ValidationError("review instructions do not match reconstructed template")
    if not _same_json(_object(review.get("source"), "review.source"), template["source"]):
        raise ValidationError("review source does not match reconstructed template")

    expected_cases = _unique_rows(template["cases"], "case_id", "template.cases")
    review_cases = _unique_rows(review.get("cases"), "case_id", "review.cases")
    if set(review_cases) != set(expected_cases):
        raise ValidationError("review case coverage does not match packet")
    case_fields = {
        "case_id",
        "user_request",
        "search_query",
        "artifact_type",
        "high_impact",
        "none_needed",
        "items",
    }
    item_fields = {
        "item_id",
        "item_number",
        "relevance",
        "canonical_current",
        "harmful_if_primary",
    }
    for case_id, expected_case in expected_cases.items():
        case = review_cases[case_id]
        _require_exact_fields(case, case_fields, f"review case {case_id}")
        for field in (
            "user_request",
            "search_query",
            "artifact_type",
            "high_impact",
        ):
            if not _same_json(case[field], expected_case[field]):
                raise ValidationError(f"review case {case_id} static fields do not match packet")
        _boolean(case.get("none_needed"), f"review case {case_id}.none_needed")
        expected_items = _unique_rows(
            expected_case.get("items"), "item_id", f"template case {case_id}.items"
        )
        items = _unique_rows(case.get("items"), "item_id", f"review case {case_id}.items")
        if set(items) != set(expected_items):
            raise ValidationError(f"review item coverage does not match packet for {case_id}")
        for item_id in expected_items:
            item = items[item_id]
            _require_exact_fields(item, item_fields, f"review item {item_id}")
            if not _same_json(item.get("item_number"), expected_items[item_id]["item_number"]):
                raise ValidationError(f"review item {item_id}.item_number does not match packet order")
            relevance = item.get("relevance")
            if type(relevance) is not int or relevance not in VALID_RELEVANCE:
                raise ValidationError(f"review item {item_id}.relevance must be 0..3")
            _boolean(
                item.get("canonical_current"),
                f"review item {item_id}.canonical_current",
            )
            _boolean(
                item.get("harmful_if_primary"),
                f"review item {item_id}.harmful_if_primary",
            )
    return reviewer, review_cases


def finalize_benchmark(
    review: JsonDict,
    packet: JsonDict,
    mapping: JsonDict,
    *,
    index_sha256: str,
    valid_artifacts: dict[str, JsonDict],
) -> JsonDict:
    """Reconstruct all static inputs and materialize only explicit human labels."""

    index_hash, packet_cases, mapping_ids = _validated_inputs(
        packet,
        mapping,
        index_sha256=index_sha256,
        valid_artifacts=valid_artifacts,
    )
    template = _review_template(packet, mapping, packet_cases, index_sha256=index_hash)
    reviewer, review_cases = _validate_labeled_review(review, template)
    mapping_key = _review_mapping_key(mapping)

    finalized_cases: list[JsonDict] = []
    for case_id, packet_case in packet_cases.items():
        review_case = review_cases[case_id]
        review_items = _unique_rows(
            review_case["items"], "item_id", f"review case {case_id}.items"
        )
        packet_items = _unique_rows(
            packet_case["items"], "item_id", f"packet case {case_id}.items"
        )
        finalized_items: list[JsonDict] = []
        for item_id in packet_items:
            review_item_id = _review_item_id(mapping_key, case_id, item_id)
            item = review_items[review_item_id]
            finalized_items.append(
                {
                    "item_id": item_id,
                    "artifact_id": mapping_ids[case_id][item_id],
                    "relevance": item["relevance"],
                    "canonical_current": item["canonical_current"],
                    "harmful_if_primary": item["harmful_if_primary"],
                }
            )
        finalized_cases.append(
            {
                "case_id": case_id,
                "user_request": packet_case["user_request"],
                "search_query": packet_case["search_query"],
                "artifact_type": packet_case.get("artifact_type"),
                "high_impact": packet_case["high_impact"],
                "none_needed": review_case["none_needed"],
                "items": finalized_items,
            }
        )
    return {
        "schema_version": 1,
        "benchmark_id": f"{template['review_id']}-human-v1",
        "human_gold_created": True,
        "reviewer": reviewer,
        "instructions": packet["instructions"],
        "source": {
            **copy.deepcopy(template["source"]),
            "review_sha256": canonical_sha256(review),
        },
        "cases": finalized_cases,
    }


def _checkout_index_module() -> Any:
    try:
        from hermes_local_knowledge import index as index_module
    except ImportError as exc:
        raise ValidationError(f"cannot load index implementation from reviewed checkout: {exc}") from exc
    expected = (REPOSITORY_ROOT / "hermes_local_knowledge" / "index.py").resolve()
    loaded = Path(index_module.__file__ or "").resolve()
    if loaded != expected:
        raise ValidationError(f"expected index implementation {expected}, got {loaded}")
    return index_module


def _index_artifacts(path: Path) -> dict[str, JsonDict]:
    index_module = _checkout_index_module()
    try:
        connection = index_module.connect_readonly(path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).casefold() != "ok":
                raise ValidationError("frozen index failed SQLite integrity_check")
            version = connection.execute("PRAGMA user_version").fetchone()
            if version is None or type(version[0]) is not int or version[0] != index_module.INDEX_FORMAT_VERSION:
                raise ValidationError("frozen index format version does not match this checkout")
            rows = connection.execute("SELECT * FROM artifacts ORDER BY id").fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise ValidationError(f"cannot read frozen index: {exc}") from exc

    artifacts: dict[str, JsonDict] = {}
    for row in rows:
        artifact = _object(index_module.decode_artifact_row(row), "frozen index artifact")
        artifact_id = _identifier(artifact.get("id"), "frozen index artifact.id")
        if artifact_id in artifacts:
            raise ValidationError(f"duplicate artifact {artifact_id!r} in frozen index")
        artifact_type = _text(artifact.get("type"), f"frozen index artifact {artifact_id}.type")
        if artifact_type not in VALID_ARTIFACT_TYPES:
            raise ValidationError(f"frozen index artifact {artifact_id}.type is invalid")
        _text(artifact.get("title"), f"frozen index artifact {artifact_id}.title")
        _text(artifact.get("path"), f"frozen index artifact {artifact_id}.path")
        artifacts[artifact_id] = artifact
    if not artifacts:
        raise ValidationError("frozen index must contain artifacts")
    return artifacts


def _preflight_private_cli(output: Path, inputs: Iterable[tuple[Path, str]]) -> list[Path]:
    _require_private_filesystem_support()
    _resolved_private_path(output, "private output")
    resolved_inputs = [_validate_private_input(path, label) for path, label in inputs]
    _reject_output_alias(output, resolved_inputs)
    return resolved_inputs


def _cli_prepare(args: argparse.Namespace) -> JsonDict:
    inputs = _preflight_private_cli(
        args.output,
        ((args.packet, "packet"), (args.mapping, "mapping"), (args.index, "index")),
    )
    packet_path, mapping_path, index_path = inputs
    packet = load_json(packet_path)
    mapping = load_json(mapping_path)
    with _frozen_index_snapshot(index_path) as snapshot:
        review = prepare_review(
            packet,
            mapping,
            index_sha256=file_sha256(snapshot),
            valid_artifacts=_index_artifacts(snapshot),
        )
    write_private_json(args.output, review)
    return {"output": str(args.output), "cases": len(review["cases"]), "human_gold": False}


def _cli_finalize(args: argparse.Namespace) -> JsonDict:
    inputs = _preflight_private_cli(
        args.output,
        (
            (args.review, "review"),
            (args.packet, "packet"),
            (args.mapping, "mapping"),
            (args.index, "index"),
        ),
    )
    review_path, packet_path, mapping_path, index_path = inputs
    review = load_json(review_path)
    packet = load_json(packet_path)
    mapping = load_json(mapping_path)
    with _frozen_index_snapshot(index_path) as snapshot:
        benchmark = finalize_benchmark(
            review,
            packet,
            mapping,
            index_sha256=file_sha256(snapshot),
            valid_artifacts=_index_artifacts(snapshot),
        )
    write_private_json(args.output, benchmark)
    return {"output": str(args.output), "cases": len(benchmark["cases"]), "human_gold": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="build an explicit human-review template")
    for name in ("packet", "mapping", "index", "output"):
        prepare.add_argument(f"--{name}", type=Path, required=True)
    prepare.set_defaults(handler=_cli_prepare)

    finalize = subparsers.add_parser("finalize", help="materialize explicit human labels")
    for name in ("review", "packet", "mapping", "index", "output"):
        finalize.add_argument(f"--{name}", type=Path, required=True)
    finalize.set_defaults(handler=_cli_finalize)
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
