from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
from typing import Any

import pytest

from hermes_local_knowledge.index import INDEX_FORMAT_VERSION
from scripts import lean_human_benchmark as benchmark_module
from scripts.lean_human_benchmark import (
    REPOSITORY_ROOT,
    ValidationError,
    _frozen_index_snapshot,
    _index_artifacts,
    _reject_output_alias,
    canonical_sha256,
    file_sha256,
    finalize_benchmark,
    load_json,
    main,
    prepare_review,
    write_private_json,
)


INDEX_SHA = "1" * 64


def packet() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "packet_id": "packet-v1",
        "rubric_sha256": "2" * 64,
        "instructions": "Review every synthetic item explicitly.",
        "cases": [
            {
                "case_id": "case-one",
                "user_request": "Find the current owner",
                "preceding_context": [],
                "search_query": "current owner",
                "artifact_type": None,
                "high_impact": True,
                "items": [
                    {
                        "item_id": "item-owner",
                        "artifact_type": "skill",
                        "title": "Current owner",
                        "summary": "Maintained procedure",
                        "source_locator": "skills/current",
                        "authority_evidence": [],
                    },
                    {
                        "item_id": "item-old",
                        "artifact_type": "doc",
                        "title": "Old plan",
                        "summary": "Historical implementation plan",
                        "source_locator": "plans/old",
                        "authority_evidence": [],
                    },
                ],
            },
            {
                "case_id": "case-none",
                "user_request": "What time is it?",
                "preceding_context": [],
                "search_query": "current time",
                "artifact_type": None,
                "high_impact": False,
                "items": [
                    {
                        "item_id": "item-noise",
                        "artifact_type": "doc",
                        "title": "Unrelated",
                        "summary": "Noise",
                        "source_locator": "docs/noise",
                        "authority_evidence": [],
                    }
                ],
            },
        ],
    }


def mapping(source_packet: dict[str, Any], *, index_sha256: str = INDEX_SHA) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "packet_id": "packet-v1",
        "packet_sha256": canonical_sha256(source_packet),
        "input_hashes": {"index_sqlite": index_sha256},
        "cases": {
            "case-one": {
                "items": {
                    "item-owner": {"artifact_id": "skill:owner"},
                    "item-old": {"artifact_id": "doc:old"},
                }
            },
            "case-none": {"items": {"item-noise": {"artifact_id": "doc:noise"}}},
        },
    }


def index_artifacts() -> dict[str, dict[str, Any]]:
    return {
        "skill:owner": {
            "id": "skill:owner",
            "type": "skill",
            "title": "Current owner",
            "summary": "Maintained procedure",
            "path": "skills/current",
        },
        "doc:old": {
            "id": "doc:old",
            "type": "doc",
            "title": "Old plan",
            "summary": "Historical implementation plan",
            "path": "plans/old",
        },
        "doc:noise": {
            "id": "doc:noise",
            "type": "doc",
            "title": "Unrelated",
            "summary": "Noise",
            "path": "docs/noise",
        },
    }


def prepared_review() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source_packet = packet()
    private_mapping = mapping(source_packet)
    review = prepare_review(
        source_packet,
        private_mapping,
        index_sha256=INDEX_SHA,
        valid_artifacts=index_artifacts(),
    )
    return review, source_packet, private_mapping


def label_review(review: dict[str, Any]) -> dict[str, Any]:
    labeled = copy.deepcopy(review)
    labeled["reviewer"] = "Alex"
    for case in labeled["cases"]:
        case["rationale"] = "Explicitly reviewed."
        case["none_needed"] = case["case_id"] == "case-none"
        for item in case["items"]:
            item_id = item["item_id"]
            item["relevance"] = 3 if item_id == "item-owner" else 0
            item["canonical_current"] = item_id == "item-owner"
            item["harmful_if_primary"] = item_id == "item-old"
    return labeled


def test_prepare_is_deterministic_explicit_and_blinded() -> None:
    source_packet = packet()
    private_mapping = mapping(source_packet)

    first = prepare_review(
        source_packet,
        private_mapping,
        index_sha256=INDEX_SHA,
        valid_artifacts=index_artifacts(),
    )
    second = prepare_review(
        source_packet,
        private_mapping,
        index_sha256=INDEX_SHA,
        valid_artifacts=index_artifacts(),
    )

    assert first == second
    assert first["human_gold_created"] is False
    assert first["reviewer"] is None
    assert first["instructions"] == source_packet["instructions"]
    assert "preceding_context" not in first["cases"][0]
    assert first["cases"][0]["none_needed"] is None
    assert first["cases"][0]["items"][0]["relevance"] is None
    assert "skill:owner" not in json.dumps(first, sort_keys=True)
    assert first["source"] == {
        "index_sha256": INDEX_SHA,
        "mapping_sha256": canonical_sha256(private_mapping),
        "packet_id": "packet-v1",
        "packet_sha256": canonical_sha256(source_packet),
        "rubric_sha256": "2" * 64,
    }


def test_prepare_and_finalize_omit_source_only_preceding_context() -> None:
    source_packet = packet()
    private_text = "synthetic transcript-shaped context that must not be copied"
    source_packet["cases"][0]["preceding_context"] = [
        {"role": "user", "content": private_text}
    ]
    private_mapping = mapping(source_packet)

    review = prepare_review(
        source_packet,
        private_mapping,
        index_sha256=INDEX_SHA,
        valid_artifacts=index_artifacts(),
    )
    assert private_text not in json.dumps(review, sort_keys=True)
    assert "preceding_context" not in review["cases"][0]

    benchmark = finalize_benchmark(
        label_review(review),
        source_packet,
        private_mapping,
        index_sha256=INDEX_SHA,
        valid_artifacts=index_artifacts(),
    )
    assert private_text not in json.dumps(benchmark, sort_keys=True)
    assert "preceding_context" not in benchmark["cases"][0]


def test_finalize_requires_explicit_human_labels_and_maps_private_ids() -> None:
    review, source_packet, private_mapping = prepared_review()
    labeled = label_review(review)

    benchmark = finalize_benchmark(
        labeled,
        source_packet,
        private_mapping,
        index_sha256=INDEX_SHA,
        valid_artifacts=index_artifacts(),
    )

    assert benchmark["human_gold_created"] is True
    assert benchmark["reviewer"] == "Alex"
    assert benchmark["instructions"] == source_packet["instructions"]
    assert "preceding_context" not in benchmark["cases"][0]
    assert benchmark["source"]["review_sha256"] == canonical_sha256(labeled)
    assert benchmark["cases"][0]["human_rationale"] == "Explicitly reviewed."
    assert benchmark["cases"][0]["items"][0] == {
        "artifact_id": "skill:owner",
        "canonical_current": True,
        "harmful_if_primary": False,
        "item_id": "item-owner",
        "relevance": 3,
    }
    assert benchmark["cases"][1]["none_needed"] is True


def test_finalize_preserves_review_hash_and_packet_order() -> None:
    review, source_packet, private_mapping = prepared_review()
    labeled = label_review(review)
    labeled["reviewer"] = "  Alex  "
    labeled["cases"][0]["rationale"] = "  Explicitly reviewed.  "
    labeled["cases"].reverse()
    labeled["cases"][1]["items"].reverse()
    original = copy.deepcopy(labeled)
    review_hash = canonical_sha256(labeled)

    benchmark = finalize_benchmark(
        labeled,
        source_packet,
        private_mapping,
        index_sha256=INDEX_SHA,
        valid_artifacts=index_artifacts(),
    )

    assert labeled == original
    assert benchmark["source"]["review_sha256"] == review_hash
    assert benchmark["reviewer"] == "Alex"
    assert [case["case_id"] for case in benchmark["cases"]] == ["case-one", "case-none"]
    assert [item["item_id"] for item in benchmark["cases"][0]["items"]] == [
        "item-owner",
        "item-old",
    ]


@pytest.mark.parametrize("field", ["relevance", "canonical_current", "harmful_if_primary"])
def test_finalize_rejects_unresolved_item_labels(field: str) -> None:
    review, source_packet, private_mapping = prepared_review()
    labeled = label_review(review)
    labeled["cases"][0]["items"][0][field] = None

    with pytest.raises(ValidationError, match=field):
        finalize_benchmark(
            labeled,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )


def test_finalize_rejects_coverage_static_field_and_shape_drift() -> None:
    review, source_packet, private_mapping = prepared_review()

    missing_case = label_review(review)
    missing_case["cases"].pop()
    with pytest.raises(ValidationError, match="case coverage"):
        finalize_benchmark(
            missing_case,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    missing_item = label_review(review)
    missing_item["cases"][0]["items"].pop()
    with pytest.raises(ValidationError, match="item coverage"):
        finalize_benchmark(
            missing_item,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    tampered_query = label_review(review)
    tampered_query["cases"][0]["search_query"] = "attacker-selected query"
    with pytest.raises(ValidationError, match="static fields"):
        finalize_benchmark(
            tampered_query,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    extra_field = label_review(review)
    extra_field["cases"][0]["typo"] = True
    with pytest.raises(ValidationError, match="fields"):
        finalize_benchmark(
            extra_field,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    tampered_instructions = label_review(review)
    tampered_instructions["instructions"] = "Different labeling contract."
    with pytest.raises(ValidationError, match="instructions"):
        finalize_benchmark(
            tampered_instructions,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )


def test_finalize_rejects_unresolved_case_and_source_identity() -> None:
    review, source_packet, private_mapping = prepared_review()

    no_reviewer = label_review(review)
    no_reviewer["reviewer"] = None
    with pytest.raises(ValidationError, match="reviewer"):
        finalize_benchmark(
            no_reviewer,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    no_case_decision = label_review(review)
    no_case_decision["cases"][0]["none_needed"] = None
    with pytest.raises(ValidationError, match="none_needed"):
        finalize_benchmark(
            no_case_decision,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    no_rationale = label_review(review)
    no_rationale["cases"][0]["rationale"] = "  "
    with pytest.raises(ValidationError, match="rationale"):
        finalize_benchmark(
            no_rationale,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    wrong_source = label_review(review)
    wrong_source["source"]["index_sha256"] = "4" * 64
    with pytest.raises(ValidationError, match="source"):
        finalize_benchmark(
            wrong_source,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )


def test_prepare_rejects_mapping_and_index_identity_failures() -> None:
    source_packet = packet()
    private_mapping = mapping(source_packet)

    wrong_hash = copy.deepcopy(private_mapping)
    wrong_hash["input_hashes"]["index_sqlite"] = "3" * 64
    with pytest.raises(ValidationError, match="index hash"):
        prepare_review(
            source_packet,
            wrong_hash,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    duplicate = copy.deepcopy(private_mapping)
    duplicate["cases"]["case-one"]["items"]["item-old"]["artifact_id"] = "skill:owner"
    with pytest.raises(ValidationError, match="duplicate artifact_id"):
        prepare_review(
            source_packet,
            duplicate,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    missing = index_artifacts()
    missing.pop("doc:old")
    with pytest.raises(ValidationError, match="not present in the frozen index"):
        prepare_review(
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=missing,
        )

    drifted = index_artifacts()
    drifted["doc:old"] = {**drifted["doc:old"], "title": "Different title"}
    with pytest.raises(ValidationError, match="does not match frozen index"):
        prepare_review(
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=drifted,
        )

    drifted_summary = index_artifacts()
    drifted_summary["doc:old"] = {
        **drifted_summary["doc:old"],
        "summary": "Reviewer-visible forged summary",
    }
    with pytest.raises(ValidationError, match="does not match frozen index"):
        prepare_review(
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=drifted_summary,
        )


def test_packet_validation_rejects_bad_context_filter_and_empty_items() -> None:
    source_packet = packet()
    private_mapping = mapping(source_packet)

    bad_context = copy.deepcopy(source_packet)
    bad_context["cases"][0]["preceding_context"] = [{"role": "system", "content": "bad"}]
    with pytest.raises(ValidationError, match="role"):
        prepare_review(
            bad_context,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    missing_instructions = copy.deepcopy(source_packet)
    missing_instructions["instructions"] = ""
    with pytest.raises(ValidationError, match="instructions"):
        prepare_review(
            missing_instructions,
            mapping(missing_instructions),
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    bad_filter = copy.deepcopy(source_packet)
    bad_filter["cases"][0]["artifact_type"] = "skills"
    with pytest.raises(ValidationError, match="artifact_type"):
        prepare_review(
            bad_filter,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    unhashable_filter = copy.deepcopy(source_packet)
    unhashable_filter["cases"][0]["artifact_type"] = ["skill"]
    with pytest.raises(ValidationError, match="artifact_type"):
        prepare_review(
            unhashable_filter,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    leaking_item = copy.deepcopy(source_packet)
    leaking_item["cases"][0]["items"][0]["artifact_id"] = "skill:owner"
    with pytest.raises(ValidationError, match="invalid fields"):
        prepare_review(
            leaking_item,
            mapping(leaking_item),
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    malformed_evidence = copy.deepcopy(source_packet)
    malformed_evidence["cases"][0]["items"][0]["authority_evidence"] = [
        {"citation": "rule", "excerpt": "", "artifact_id": "skill:owner"}
    ]
    with pytest.raises(ValidationError, match="invalid fields"):
        prepare_review(
            malformed_evidence,
            mapping(malformed_evidence),
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    empty = copy.deepcopy(source_packet)
    empty["cases"][0]["items"] = []
    with pytest.raises(ValidationError, match="items"):
        prepare_review(
            empty,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    boolean_schema = copy.deepcopy(source_packet)
    boolean_schema["schema_version"] = True
    with pytest.raises(ValidationError, match="schema_version"):
        prepare_review(
            boolean_schema,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )

    padded_identity = copy.deepcopy(source_packet)
    padded_identity["cases"][0]["case_id"] = "case-one "
    with pytest.raises(ValidationError, match="surrounding whitespace"):
        prepare_review(
            padded_identity,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )


def test_finalize_uses_canonical_static_field_equality() -> None:
    review, source_packet, private_mapping = prepared_review()
    labeled = label_review(review)
    labeled["cases"][0]["high_impact"] = 1

    with pytest.raises(ValidationError, match="static fields"):
        finalize_benchmark(
            labeled,
            source_packet,
            private_mapping,
            index_sha256=INDEX_SHA,
            valid_artifacts=index_artifacts(),
        )


def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value":NaN}', encoding="utf-8")
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")

    with pytest.raises(ValidationError, match="duplicate JSON key"):
        load_json(duplicate)
    with pytest.raises(ValidationError, match="non-finite"):
        load_json(nonfinite)
    with pytest.raises(ValidationError, match="cannot read JSON"):
        load_json(invalid_utf8)
    with pytest.raises(ValidationError, match="JSON-serializable"):
        canonical_sha256({"value": float("nan")})


def test_private_writer_uses_restrictive_modes_and_rejects_repository_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "private" / "result.json"
    if os.name == "nt":
        with pytest.raises(ValidationError, match="unsupported on Windows"):
            write_private_json(destination, {"ok": True})
        return

    write_private_json(destination, {"ok": True})
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    repository_path = REPOSITORY_ROOT / "PRIVATE" / "result.json"
    with pytest.raises(ValidationError, match="outside the public repository"):
        write_private_json(repository_path, {"private": True})
    assert not repository_path.exists()

    sibling_worktree = tmp_path / "sibling-worktree"
    sibling_worktree.mkdir()
    monkeypatch.setattr(
        benchmark_module,
        "_repository_roots",
        lambda: (REPOSITORY_ROOT, sibling_worktree.resolve()),
    )
    sibling_path = sibling_worktree / "PRIVATE" / "result.json"
    with pytest.raises(ValidationError, match="outside the public repository"):
        write_private_json(sibling_path, {"private": True})
    assert not sibling_path.exists()


def test_repository_enumeration_strips_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = benchmark_module.subprocess.run
    observed_env: dict[str, str] = {}

    def inspect_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        observed_env.update(env)
        return real_run(*args, **kwargs)

    monkeypatch.setenv("GIT_DIR", "/tmp/unrelated.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/unrelated-worktree")
    monkeypatch.setattr(benchmark_module.subprocess, "run", inspect_run)

    assert REPOSITORY_ROOT in benchmark_module._repository_roots()
    assert not any(key.startswith("GIT_") for key in observed_env)


def test_private_writer_rejects_insecure_existing_directory(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode contract")
    destination = tmp_path / "shared" / "result.json"
    destination.parent.mkdir(mode=0o755)
    destination.parent.chmod(0o755)

    with pytest.raises(ValidationError, match="mode 0700"):
        write_private_json(destination, {"private": True})
    assert not destination.exists()


def test_frozen_index_snapshot_is_private_and_survives_source_replacement(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX snapshot contract")
    source = tmp_path / "index.sqlite"
    source.write_bytes(b"frozen")

    with _frozen_index_snapshot(source) as snapshot:
        source.write_bytes(b"replacement")
        assert snapshot.read_bytes() == b"frozen"
        assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_frozen_index_snapshot_rejects_sibling_worktree_tmpdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX snapshot contract")
    source = tmp_path / "index.sqlite"
    source.write_bytes(b"private")
    sibling_worktree = tmp_path / "sibling-worktree"
    sibling_worktree.mkdir()
    monkeypatch.setattr(
        benchmark_module,
        "_repository_roots",
        lambda: (REPOSITORY_ROOT, sibling_worktree.resolve()),
    )
    monkeypatch.setattr(benchmark_module.tempfile, "gettempdir", lambda: str(sibling_worktree))

    with pytest.raises(ValidationError, match="outside the public repository"):
        with _frozen_index_snapshot(source):
            pass
    assert not any(sibling_worktree.iterdir())


def test_frozen_index_snapshot_fails_before_copying_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "index.sqlite"
    source.write_bytes(b"private")

    def unexpected_copy(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("snapshot copy started")

    monkeypatch.setattr(benchmark_module.os, "name", "nt")
    monkeypatch.setattr(benchmark_module.tempfile, "TemporaryDirectory", unexpected_copy)
    with pytest.raises(ValidationError, match="unsupported on Windows"):
        with _frozen_index_snapshot(source):
            pass


def test_output_must_not_alias_an_input(tmp_path: Path) -> None:
    source = tmp_path / "review.json"
    source.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="must not alias"):
        _reject_output_alias(source, (source,))

    alias = tmp_path / "alias.json"
    alias.hardlink_to(source)
    with pytest.raises(ValidationError, match="must not alias"):
        _reject_output_alias(alias, (source,))


def create_index(path: Path, *, format_version: int = INDEX_FORMAT_VERSION) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={format_version}")
        connection.execute(
            "CREATE TABLE artifacts ("
            "id TEXT PRIMARY KEY, type TEXT, title TEXT, summary TEXT, path TEXT, "
            "triggers_json TEXT, entities_json TEXT, related_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, '[]', '[]', '[]')",
            [
                (
                    artifact["id"],
                    artifact["type"],
                    artifact["title"],
                    artifact["summary"],
                    artifact["path"],
                )
                for artifact in index_artifacts().values()
            ],
        )


def write_secure_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def test_index_reader_rejects_wrong_format_version(tmp_path: Path) -> None:
    index_path = tmp_path / "old-index.sqlite"
    create_index(index_path, format_version=0)

    with pytest.raises(ValidationError, match="format version"):
        _index_artifacts(index_path)


def test_cli_prepare_and_finalize_real_private_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    if os.name == "nt":
        pytest.skip("POSIX private-file contract")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    index_path = workspace / "index.sqlite"
    create_index(index_path)
    index_path.chmod(0o600)
    source_packet = packet()
    private_mapping = mapping(source_packet, index_sha256=file_sha256(index_path))
    packet_path = workspace / "packet.json"
    mapping_path = workspace / "mapping.json"
    review_path = workspace / "review.json"
    labeled_path = workspace / "review-labeled.json"
    benchmark_path = workspace / "benchmark.json"
    write_secure_json(packet_path, source_packet)
    write_secure_json(mapping_path, private_mapping)

    assert main(
        [
            "prepare",
            "--packet",
            str(packet_path),
            "--mapping",
            str(mapping_path),
            "--index",
            str(index_path),
            "--output",
            str(review_path),
        ]
    ) == 0
    prepared = load_json(review_path)
    write_private_json(labeled_path, label_review(prepared))

    assert main(
        [
            "finalize",
            "--review",
            str(labeled_path),
            "--packet",
            str(packet_path),
            "--mapping",
            str(mapping_path),
            "--index",
            str(index_path),
            "--output",
            str(benchmark_path),
        ]
    ) == 0

    benchmark = load_json(benchmark_path)
    assert benchmark["human_gold_created"] is True
    assert len(benchmark["cases"]) == 2
    assert capsys.readouterr().out.count('"output"') == 2
    assert _index_artifacts(index_path)["skill:owner"]["title"] == "Current owner"
