from __future__ import annotations

from pathlib import Path

import pytest

from hermes_local_knowledge import index as index_owner
from hermes_local_knowledge.artifacts import Artifact


def build_fixture_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifacts: list[Artifact],
) -> Path:
    root = tmp_path / "root"
    hermes_home = tmp_path / "hermes-home"
    state_dir = tmp_path / "state"
    root.mkdir()
    hermes_home.mkdir()

    def collect_fixture(*_args: object, **_kwargs: object) -> list[Artifact]:
        return list(artifacts)

    monkeypatch.setattr(index_owner, "collect_artifacts", collect_fixture)
    built = index_owner.build_index(root, state_dir, hermes_home)
    assert built is not None
    assert {artifact.id for artifact in built[0]} == {artifact.id for artifact in artifacts}
    return state_dir / "index.sqlite"


def test_search_collects_beyond_three_hundred_rows_and_filters_type_before_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def matching_artifact(*, artifact_id: str, artifact_type: str, path: str) -> Artifact:
        return Artifact(
            id=artifact_id,
            type=artifact_type,
            title="paperless reviewer helper",
            path=path,
            summary="Paperless reviewer helper.",
            triggers=["paperless", "reviewer"],
            search_text="paperless reviewer mcp",
        )

    noise = [
        matching_artifact(
            artifact_id=f"skill:paperless-reviewer-noise-{index:03d}",
            artifact_type="skill",
            path=f"skills/noise-{index:03d}",
        )
        for index in range(321)
    ]
    target = matching_artifact(
        artifact_id="script:paperless-reviewer-target",
        artifact_type="script",
        path="scripts/target.py",
    )
    db_path = build_fixture_index(tmp_path, monkeypatch, [*noise, target])

    ranked = index_owner.search_index(db_path, "paperless reviewer mcp", limit=20)
    assert ranked[0]["id"] == target.id

    unfiltered = index_owner.search_index(db_path, "paperless reviewer", limit=10)
    filtered = index_owner.search_index(
        db_path,
        "paperless reviewer",
        limit=1,
        artifact_type="script",
    )
    assert target.id not in {row["id"] for row in unfiltered}
    assert [row["id"] for row in filtered] == [target.id]


def test_metadata_identity_enters_a_full_strict_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "identity recovery"
    strict_noise = [
        Artifact(
            id=f"runbook:identity-prose-{index:03d}",
            type="runbook",
            title=f"prose note {index:03d}",
            path=f"docs/prose-{index:03d}.md",
            summary="Broad operational notes.",
            search_text=query,
        )
        for index in range(325)
    ]
    target = Artifact(
        id="script:identity-recovery-wrapper",
        type="script",
        title="generic wrapper",
        path="scripts/generic/run.sh",
        summary="Generic wrapper.",
        triggers=["generic", "wrapper"],
        search_text="generic wrapper",
    )
    db_path = build_fixture_index(tmp_path, monkeypatch, [*strict_noise, target])

    results = index_owner.search_index(db_path, query, limit=5)

    assert len(results) == 5
    assert results[0]["id"] == target.id
    assert "metadata_score" not in results[0]


@pytest.mark.parametrize("query", ['foo"bar baz"', '"bar baz"foo'])
def test_adjacent_quoted_phrases_keep_order_on_a_full_strict_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    exact = [
        Artifact(
            id=f"runbook:ordered-{index}",
            type="runbook",
            title=f"ordered phrase {index}",
            path=f"docs/ordered-{index}.md",
            summary="Foo bar baz appears together.",
            search_text="foo bar baz",
        )
        for index in range(6)
    ]
    distractor = Artifact(
        id="runbook:foo-bar-baz-distractor",
        type="runbook",
        title="foo baz bar",
        path="docs/foo-baz-bar.md",
        summary="All terms appear in the wrong order.",
        search_text="foo baz bar",
    )
    db_path = build_fixture_index(tmp_path, monkeypatch, [*exact, distractor])

    results = index_owner.search_index(db_path, query, limit=5)

    assert len(results) == 5
    assert {row["id"] for row in results} <= {artifact.id for artifact in exact}
    assert distractor.id not in {row["id"] for row in results}


@pytest.mark.parametrize(
    ("query", "expected_ids"),
    [
        ('"unique exact phrase"', {"runbook:exact-phrase"}),
        ('updates "what next"', {"runbook:update-only", "runbook:phrase-only"}),
    ],
)
def test_quoted_fallback_is_strict_for_pure_phrases_and_relaxed_for_mixed_queries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    expected_ids: set[str],
) -> None:
    artifacts = [
        Artifact(
            id="runbook:exact-phrase",
            type="runbook",
            title="unique exact phrase",
            path="docs/exact.md",
            summary="A unique exact phrase appears here.",
            search_text="unique exact phrase",
        ),
        Artifact(
            id="runbook:relaxed-phrase-noise",
            type="runbook",
            title="unique phrase",
            path="docs/noise.md",
            summary="Only two phrase tokens appear.",
            search_text="unique phrase",
        ),
        Artifact(
            id="runbook:update-only",
            type="runbook",
            title="Update status",
            path="docs/update.md",
            summary="Release update status.",
            search_text="release update status",
        ),
        Artifact(
            id="runbook:phrase-only",
            type="runbook",
            title="Next steps",
            path="docs/next.md",
            summary="What next guidance.",
            search_text="what next guidance",
        ),
    ]
    db_path = build_fixture_index(tmp_path, monkeypatch, artifacts)

    results = index_owner.search_index(db_path, query, limit=5)

    assert {row["id"] for row in results} == expected_ids
