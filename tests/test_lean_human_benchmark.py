from __future__ import annotations

import copy
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any

import pytest

from scripts.lean_human_benchmark import (
    _frozen_index_snapshot,
    _index_artifact_ids,
    _reject_output_alias,
    ValidationError,
    apply_explicit_supersession,
    build_candidate_rankings,
    canonical_sha256,
    compare_rankings,
    finalize_benchmark,
    prepare_review,
    replay_benchmark,
    write_private_json,
)


INDEX_SHA = "1" * 64


def packet() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "packet_id": "packet-v1",
        "rubric_sha256": "2" * 64,
        "instructions": {"purpose": "synthetic"},
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


def mapping(source_packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "packet_id": "packet-v1",
        "packet_sha256": canonical_sha256(source_packet),
        "input_hashes": {"index_sqlite": INDEX_SHA},
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


def annotation(source_packet: dict[str, Any], *, second: bool = False) -> dict[str, Any]:
    owner_relevance = 2 if second else 3
    return {
        "schema_version": 1,
        "annotator": "rater-b" if second else "rater-a",
        "independent": True,
        "packet_sha256": canonical_sha256(source_packet),
        "judgments": [
            {
                "case_id": "case-one",
                "none_needed": "no",
                "case_confidence": "high",
                "case_rationale": "A maintained owner exists.",
                "items": [
                    {
                        "item_id": "item-owner",
                        "relevance": owner_relevance,
                        "role": "canonical_owner",
                        "lifecycle": "current",
                        "harmful_if_primary": False,
                        "harm_reasons": [],
                        "confidence": "high",
                        "rationale": "The maintained owner.",
                    },
                    {
                        "item_id": "item-old",
                        "relevance": 1,
                        "role": "background",
                        "lifecycle": "historical",
                        "harmful_if_primary": True,
                        "harm_reasons": ["stale"],
                        "confidence": "high",
                        "rationale": "Historical only.",
                    },
                ],
            },
            {
                "case_id": "case-none",
                "none_needed": "yes",
                "case_confidence": "high",
                "case_rationale": "A live tool is required.",
                "items": [
                    {
                        "item_id": "item-noise",
                        "relevance": 0,
                        "role": "irrelevant",
                        "lifecycle": "unknown",
                        "harmful_if_primary": False,
                        "harm_reasons": [],
                        "confidence": "high",
                        "rationale": "Not useful.",
                    }
                ],
            },
        ],
    }


def prepared_review() -> tuple[dict[str, Any], dict[str, Any]]:
    source_packet = packet()
    private_mapping = mapping(source_packet)
    review = prepare_review(
        source_packet,
        private_mapping,
        annotation(source_packet),
        annotation(source_packet, second=True),
        index_sha256=INDEX_SHA,
    )
    return review, private_mapping


def accepted_decisions(review: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "review_sha256": canonical_sha256(review),
        "reviewer": "Alex",
        "cases": [
            {
                "case_id": "case-one",
                "status": "accepted_proposal",
                "rationale": "Confirmed.",
                "none_needed": None,
                "overrides": [],
            },
            {
                "case_id": "case-none",
                "status": "accepted_proposal",
                "rationale": "Confirmed.",
                "none_needed": None,
                "overrides": [],
            },
        ],
    }


def finalize_for_test(
    review: dict[str, Any],
    decisions: dict[str, Any],
    private_mapping: dict[str, Any],
    *,
    valid_artifact_ids: set[str] | None = None,
) -> dict[str, Any]:
    source_packet = packet()
    return finalize_benchmark(
        review,
        decisions,
        source_packet,
        private_mapping,
        annotation(source_packet),
        annotation(source_packet, second=True),
        index_sha256=INDEX_SHA,
        valid_artifact_ids=valid_artifact_ids
        or {"skill:owner", "doc:old", "doc:noise"},
    )


def test_prepare_review_is_deterministic_and_conservative() -> None:
    source_packet = packet()
    private_mapping = mapping(source_packet)
    left = annotation(source_packet)
    right = annotation(source_packet, second=True)

    first = prepare_review(source_packet, private_mapping, left, right, index_sha256=INDEX_SHA)
    second = prepare_review(source_packet, private_mapping, left, right, index_sha256=INDEX_SHA)

    assert first == second
    assert first["human_gold_created"] is False
    owner = first["cases"][0]["proposal"]["items"][0]
    assert owner["relevance"] == 3
    assert owner["canonical_current"] is True
    assert owner["harmful_if_primary"] is False
    assert owner["disagreements"] == ["relevance"]
    old = first["cases"][0]["proposal"]["items"][1]
    assert old["harmful_if_primary"] is True
    assert first["cases"][1]["proposal"]["none_needed"] is True


def test_prepare_review_rejects_incomplete_annotation_and_wrong_index() -> None:
    source_packet = packet()
    private_mapping = mapping(source_packet)
    left = annotation(source_packet)
    right = annotation(source_packet, second=True)
    right["judgments"][0]["items"].pop()

    with pytest.raises(ValidationError, match="item coverage"):
        prepare_review(source_packet, private_mapping, left, right, index_sha256=INDEX_SHA)

    with pytest.raises(ValidationError, match="index hash"):
        prepare_review(
            source_packet,
            private_mapping,
            annotation(source_packet),
            annotation(source_packet, second=True),
            index_sha256="3" * 64,
        )


def test_prepare_review_requires_distinct_raters_and_valid_case_filters() -> None:
    source_packet = packet()
    private_mapping = mapping(source_packet)
    left = annotation(source_packet)
    right = annotation(source_packet, second=True)

    with pytest.raises(ValidationError, match="annotation inputs must be distinct"):
        prepare_review(
            source_packet,
            private_mapping,
            left,
            copy.deepcopy(left),
            index_sha256=INDEX_SHA,
        )

    duplicate_annotator = copy.deepcopy(right)
    duplicate_annotator["annotator"] = left["annotator"]
    with pytest.raises(ValidationError, match="annotators must be distinct"):
        prepare_review(
            source_packet,
            private_mapping,
            left,
            duplicate_annotator,
            index_sha256=INDEX_SHA,
        )

    bad_type_packet = copy.deepcopy(source_packet)
    bad_type_packet["cases"][0]["artifact_type"] = "skills"
    with pytest.raises(ValidationError, match="artifact_type"):
        prepare_review(
            bad_type_packet,
            private_mapping,
            left,
            right,
            index_sha256=INDEX_SHA,
        )

    bad_type_packet["cases"][0]["artifact_type"] = ["skill"]
    with pytest.raises(ValidationError, match="artifact_type"):
        prepare_review(
            bad_type_packet,
            private_mapping,
            left,
            right,
            index_sha256=INDEX_SHA,
        )


def test_prepare_rejects_duplicate_artifact_mapping() -> None:
    source_packet = packet()
    private_mapping = mapping(source_packet)
    private_mapping["cases"]["case-one"]["items"]["item-old"]["artifact_id"] = "skill:owner"

    with pytest.raises(ValidationError, match="duplicate artifact_id"):
        prepare_review(
            source_packet,
            private_mapping,
            annotation(source_packet),
            annotation(source_packet, second=True),
            index_sha256=INDEX_SHA,
        )


def test_finalize_requires_all_cases_and_translates_private_handles() -> None:
    review, private_mapping = prepared_review()
    decisions = accepted_decisions(review)
    decisions["cases"][0] = {
        "case_id": "case-one",
        "status": "edited",
        "rationale": "The owner is relevant but not uniquely canonical for this wording.",
        "none_needed": False,
        "overrides": [
            {
                "item_id": "item-owner",
                "relevance": 2,
                "canonical_current": False,
                "harmful_if_primary": False,
            }
        ],
    }

    benchmark = finalize_for_test(review, decisions, private_mapping)

    assert benchmark["human_gold_created"] is True
    case = benchmark["cases"][0]
    assert case["human_status"] == "edited"
    assert case["items"][0] == {
        "artifact_id": "skill:owner",
        "canonical_current": False,
        "harmful_if_primary": False,
        "item_id": "item-owner",
        "relevance": 2,
    }
    assert case["items"][1]["artifact_id"] == "doc:old"

    incomplete = copy.deepcopy(decisions)
    incomplete["cases"].pop()
    with pytest.raises(ValidationError, match="case coverage"):
        finalize_for_test(review, incomplete, private_mapping)


def test_finalize_rejects_binding_and_identity_failures() -> None:
    review, private_mapping = prepared_review()
    decisions = accepted_decisions(review)

    decisions["review_sha256"] = "4" * 64
    with pytest.raises(ValidationError, match="review hash"):
        finalize_for_test(review, decisions, private_mapping)

    decisions = accepted_decisions(review)
    private_mapping["cases"]["case-one"]["items"]["item-old"]["artifact_id"] = "doc:missing"
    with pytest.raises(ValidationError, match="reconstructed review"):
        finalize_for_test(review, decisions, private_mapping)

    review, private_mapping = prepared_review()
    decisions = accepted_decisions(review)
    with pytest.raises(ValidationError, match="frozen index"):
        finalize_for_test(
            review,
            decisions,
            private_mapping,
            valid_artifact_ids={"skill:owner", "doc:noise"},
        )


def test_finalize_reconstructs_review_from_bound_sources() -> None:
    review, private_mapping = prepared_review()
    tampered_review = copy.deepcopy(review)
    tampered_review["cases"][0]["search_query"] = "attacker-selected query"
    decisions = accepted_decisions(tampered_review)

    with pytest.raises(ValidationError, match="reconstructed review"):
        finalize_for_test(tampered_review, decisions, private_mapping)


def test_explicit_supersession_moves_only_the_target_and_rejects_cycles() -> None:
    records = [
        {
            "artifact_id": "doc:old",
            "status": "historical",
            "confidence": "high",
            "policy_eligible": True,
            "relationships": [
                {"kind": "superseded_by", "target_artifact_id": "skill:owner"}
            ],
        }
    ]

    output, changes = apply_explicit_supersession(
        ["doc:old", "doc:unrelated", "skill:owner", "doc:last"], records
    )

    assert output == ["skill:owner", "doc:old", "doc:unrelated", "doc:last"]
    assert changes == [{"source": "doc:old", "target": "skill:owner"}]
    assert apply_explicit_supersession(["doc:unrelated", "skill:owner"], records)[0] == [
        "doc:unrelated",
        "skill:owner",
    ]

    cyclic = [
        {
            "artifact_id": "a",
            "status": "historical",
            "confidence": "high",
            "policy_eligible": True,
            "relationships": [{"kind": "superseded_by", "target_artifact_id": "b"}],
        },
        {
            "artifact_id": "b",
            "status": "historical",
            "confidence": "high",
            "policy_eligible": True,
            "relationships": [{"kind": "superseded_by", "target_artifact_id": "a"}],
        },
    ]
    with pytest.raises(ValidationError, match="cycle"):
        apply_explicit_supersession(["a", "b"], cyclic)


def test_replay_candidate_and_comparison_are_same_membership_and_decision_focused() -> None:
    review, private_mapping = prepared_review()
    benchmark = finalize_for_test(
        review,
        accepted_decisions(review),
        private_mapping,
    )

    def search(query: str, artifact_type: str | None, limit: int) -> list[str]:
        assert limit == 10
        if query == "current owner":
            return ["doc:old", "skill:owner"]
        assert artifact_type is None
        return ["doc:noise"]

    baseline = replay_benchmark(benchmark, search)
    authority = {
        "records": [
            {
                "artifact_id": "doc:old",
                "status": "historical",
                "confidence": "high",
                "policy_eligible": True,
                "relationships": [
                    {"kind": "superseded_by", "target_artifact_id": "skill:owner"}
                ],
            }
        ]
    }
    candidate = build_candidate_rankings(baseline, authority)
    report = compare_rankings(benchmark, baseline, authority)

    assert report["baseline"]["acceptable_hit_at_1"] == 0
    assert report["candidate"]["acceptable_hit_at_1"] == 1
    assert report["candidate"]["canonical_current_at_1"] == 1
    assert report["baseline"]["harmful_at_1"] == 1
    assert report["candidate"]["harmful_at_1"] == 0
    assert report["candidate_policy"] == "explicit-high-confidence-superseded-by-v1"
    assert report["baseline_sha256"] == canonical_sha256(baseline)
    assert report["authority_sha256"] == canonical_sha256(authority)
    assert report["candidate_sha256"] == canonical_sha256(candidate)
    assert report["changed_top_1"] == [
        {"baseline": "doc:old", "candidate": "skill:owner", "case_id": "case-one"}
    ]
    assert report["manual_review_required"] is True

    tampered = copy.deepcopy(baseline)
    tampered["cases"][0]["ids"].append("doc:new")
    tampered["limit"] = 2
    with pytest.raises(ValidationError, match="limit"):
        compare_rankings(benchmark, tampered, authority)

    with pytest.raises(ValidationError, match="at least 3"):
        replay_benchmark(benchmark, search, limit=2)

    malformed_benchmark = copy.deepcopy(benchmark)
    malformed_benchmark["cases"][0]["items"][0]["relevance"] = "3"
    with pytest.raises(ValidationError, match="relevance"):
        replay_benchmark(malformed_benchmark, search)

    malformed_benchmark = copy.deepcopy(benchmark)
    malformed_benchmark["cases"][0]["items"][0]["canonical_current"] = 1
    with pytest.raises(ValidationError, match="canonical_current"):
        replay_benchmark(malformed_benchmark, search)

    malformed_benchmark = copy.deepcopy(benchmark)
    malformed_benchmark["cases"][0]["artifact_type"] = ["skill"]
    with pytest.raises(ValidationError, match="artifact_type"):
        replay_benchmark(malformed_benchmark, search)


def test_private_writer_uses_restrictive_modes(tmp_path: Path) -> None:
    destination = tmp_path / "private" / "result.json"
    write_private_json(destination, {"ok": True})

    if os.name != "nt":
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX directory modes")
def test_private_writer_rejects_insecure_existing_directory(tmp_path: Path) -> None:
    destination = tmp_path / "shared" / "result.json"
    destination.parent.mkdir(mode=0o755)
    destination.parent.chmod(0o755)

    with pytest.raises(ValidationError, match="mode 0700"):
        write_private_json(destination, {"private": True})
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o755
    assert not destination.exists()


def test_frozen_index_snapshot_survives_source_replacement(tmp_path: Path) -> None:
    source = tmp_path / "index.sqlite"
    source.write_bytes(b"frozen")

    with _frozen_index_snapshot(source) as snapshot:
        source.write_bytes(b"replacement")
        assert snapshot.read_bytes() == b"frozen"


def test_index_reader_handles_sqlite_uri_characters(tmp_path: Path) -> None:
    index = tmp_path / "frozen?#index.sqlite"
    with sqlite3.connect(index) as connection:
        connection.execute("CREATE TABLE artifacts (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO artifacts (id) VALUES ('skill:owner')")

    assert _index_artifact_ids(index) == {"skill:owner"}


def test_output_must_not_alias_an_input(tmp_path: Path) -> None:
    source = tmp_path / "benchmark.json"
    source.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="must not alias"):
        _reject_output_alias(source, (source,))

    alias = tmp_path / "alias.json"
    alias.hardlink_to(source)
    with pytest.raises(ValidationError, match="must not alias"):
        _reject_output_alias(alias, (source,))


def test_private_writer_rejects_public_repository_paths() -> None:
    repository_path = Path(__file__).resolve().parents[1] / "PRIVATE" / "result.json"
    with pytest.raises(ValidationError, match="outside the public repository"):
        write_private_json(repository_path, {"private": True})
    assert not repository_path.exists()
