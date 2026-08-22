from __future__ import annotations

import os
import random
import sqlite3
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
    root.mkdir(parents=True)
    hermes_home.mkdir(parents=True)

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


def artifact(
    artifact_id: str,
    artifact_type: str,
    title: str,
    *,
    path: str | None = None,
    summary: str = "Orchid backup docs guidance.",
    entities: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
    search_text: str | None = None,
) -> Artifact:
    return Artifact(
        id=artifact_id,
        type=artifact_type,
        title=title,
        path=path or f"fixtures/{artifact_id.replace(':', '-')}.md",
        summary=summary,
        entities=list(entities),
        related=list(related),
        search_text=search_text or f"{title} {summary}",
    )


ALIASES = (
    "runbook runbooks skill skills doc docs document documents documentation "
    "reference references memory memories"
)


@pytest.mark.parametrize(
    ("noun", "target_type", "target_id"),
    [
        ("runbook", "runbook", "runbook:target"),
        ("RUNBOOKS!", "runbook", "runbook:target"),
        ("skill", "skill", "skill:target"),
        ("SKILLS.", "skill", "skill:target"),
        ("doc", "doc", "doc:target"),
        ("DOCS?", "doc", "doc:target"),
        ("document", "doc", "doc:target"),
        ("DOCUMENTS;", "doc", "doc:target"),
        ("DOCUMENTATION!", "doc", "doc:target"),
        ("reference", "doc", "doc:target"),
        ("REFERENCES:", "doc", "doc:target"),
        ("memory", "memory_doc", "memory:target"),
        ("MEMORIES!", "memory_doc", "memory:target"),
    ],
)
def test_terminal_exact_noun_alias_promotes_requested_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    noun: str,
    target_type: str,
    target_id: str,
) -> None:
    rows = [
        artifact("skill:baseline", "skill", "Orchid backup", search_text=f"orchid backup {ALIASES}"),
        artifact(
            target_id,
            target_type,
            "Zulu Orchid operations",
            entities=("Orchid",),
            search_text=f"orchid backup operations {ALIASES}",
        ),
    ]
    db = build_fixture_index(tmp_path, monkeypatch, rows)
    assert index_owner.search_index(db, f"orchid backup {noun}", limit=2)[0]["id"] == target_id


@pytest.mark.parametrize(
    "query",
    [
        "orchid runbook backup",
        "orchid backup runbook skill",
        "orchid backup runbooking",
        "orchid backup runbook.md",
        '"orchid backup runbook"',
    ],
)
def test_alias_gating_is_terminal_exact_single_and_unquoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    rows = [
        artifact("skill:gating", "skill", query.title(), summary=query, search_text=query),
        artifact(
            "runbook:target",
            "runbook",
            "Zulu Orchid operations",
            entities=("Orchid",),
            search_text=f"orchid backup documentation runbooking md {ALIASES}",
        ),
    ]
    db = build_fixture_index(tmp_path, monkeypatch, rows)
    assert index_owner.search_index(db, query, limit=2)[0]["id"] == "skill:gating"


@pytest.mark.parametrize(
    ("name", "query", "baseline", "target"),
    [
        (
            "body",
            "orchid backup runbook",
            artifact("skill:body", "skill", "Orchid backup runbook"),
            artifact(
                "runbook:lotus",
                "runbook",
                "Lotus backup",
                path="docs/lotus.md",
                summary="Do not use for Orchid backup.",
                entities=("Orchid",),
            ),
        ),
        (
            "partial-query",
            "home backup runbook",
            artifact("skill:partial-query", "skill", "Home backup runbook"),
            artifact(
                "runbook:home-assistant",
                "runbook",
                "Home Assistant backup",
                path="docs/home-assistant-backup.md",
                summary="Home Assistant backup instructions.",
                entities=("Home Assistant",),
            ),
        ),
        (
            "partial-identity",
            "home assistant backup runbook",
            artifact("skill:partial-identity", "skill", "Home Assistant backup runbook"),
            artifact(
                "runbook:home",
                "runbook",
                "Home backup",
                path="docs/home-backup.md",
                summary="Home Assistant backup instructions.",
                entities=("Home Assistant",),
            ),
        ),
    ],
)
def test_entity_provenance_rejects_body_only_and_partial_multiword_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    query: str,
    baseline: Artifact,
    target: Artifact,
) -> None:
    db = build_fixture_index(tmp_path / name, monkeypatch, [baseline, target])
    assert [row["id"] for row in index_owner.search_index(db, query, limit=2)] == [baseline.id, target.id]


def test_support_siblings_do_not_share_identity_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = artifact("skill:orchid", "skill", "Orchid backup skill")
    rows = [
        owner,
        artifact(
            "skill-support:orchid:baseline",
            "skill_support_doc",
            "Alpha Orchid backup reference",
            related=(owner.id,),
            search_text="orchid backup docs reference",
        ),
        artifact(
            "skill-support:orchid:identity",
            "skill_support_doc",
            "Orchid compatibility note",
            summary="Compatibility documentation.",
            entities=("Orchid",),
            related=(owner.id,),
            search_text="orchid backup docs compatibility",
        ),
        artifact(
            "skill-support:lotus-negative",
            "skill_support_doc",
            "Lotus backup",
            path="shared/lotus-backup.md",
            summary="Do not use for Orchid backup.",
            entities=("Orchid",),
            related=(owner.id,),
            search_text="lotus backup orchid docs",
        ),
    ]
    db = build_fixture_index(tmp_path, monkeypatch, rows)
    assert [row["id"] for row in index_owner.search_index(db, "orchid backup docs", limit=4)] == [
        owner.id,
        "skill-support:orchid:baseline",
    ]


def test_two_eligible_families_preserve_exact_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        artifact("skill:baseline", "skill", "Orchid backup runbook guide"),
        artifact("runbook:a", "runbook", "A Orchid backup", entities=("Orchid",)),
        artifact("runbook:b", "runbook", "B Orchid backup", entities=("Orchid",)),
    ]
    db = build_fixture_index(tmp_path, monkeypatch, rows)
    assert [row["id"] for row in index_owner.search_index(db, "orchid backup runbook", limit=3)] == [
        "runbook:a",
        "runbook:b",
        "skill:baseline",
    ]


def test_rejected_explicit_intent_is_exact_bounded_legacy_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terms = ["orchid", "backup", "runbook", "guide", "restore", "alpha", "zulu", "notes"]
    rng = random.Random(0)
    rows = []
    for number in range(240):
        title_terms = rng.sample(terms, rng.randint(1, 5))
        summary_terms = rng.sample(terms, rng.randint(1, 6))
        path_terms = rng.sample(terms, rng.randint(1, 4))
        rows.append(
            artifact(
                f"skill:item-{number:03d}",
                "skill",
                " ".join(title_terms),
                path=f"fixtures/{'-'.join(path_terms)}-{number:03d}.md",
                summary=" ".join(summary_terms),
                search_text=" ".join(
                    title_terms + summary_terms + rng.sample(terms, rng.randint(0, 5))
                ),
            )
        )
    db = build_fixture_index(tmp_path, monkeypatch, rows)
    explicit = index_owner.search_index(db, "orchid backup runbook", limit=5)

    monkeypatch.setattr(index_owner, "_explicit_type_request", lambda *_args, **_kwargs: None)
    baseline = index_owner.search_index(db, "orchid backup runbook", limit=5)

    assert [row["id"] for row in explicit] == [row["id"] for row in baseline]


def test_filter_and_quotes_preserve_strict_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        artifact("skill:strict", "skill", "Orchid backup runbook"),
        artifact("runbook:a", "runbook", "Orchid backup runbook A", entities=("Orchid",)),
        artifact("runbook:b", "runbook", "Orchid backup runbook B", entities=("Orchid",)),
    ]
    db = build_fixture_index(tmp_path, monkeypatch, rows)
    filtered = index_owner.search_index(db, "orchid backup runbook", limit=3, artifact_type="runbook")
    quoted = index_owner.search_index(db, '"orchid backup runbook"', limit=3)
    assert [row["id"] for row in filtered] == ["runbook:a", "runbook:b"]
    assert [row["id"] for row in quoted] == ["runbook:a", "runbook:b", "skill:strict"]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_explicit_promotion_is_prefix_consistent_past_candidate_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambiguous: bool,
) -> None:
    rows = [artifact("skill:baseline", "skill", "Orchid backup")]
    rows += [
        artifact(f"runbook:noise-{number:03d}", "runbook", f"{number:03d} Orchid guide")
        for number in range(125)
    ]
    rows.append(
        artifact(
            "runbook:zz-target",
            "runbook",
            "ZZ Orchid operations",
            entities=("Orchid",),
            summary="Orchid backup runbook target.",
        )
    )
    if ambiguous:
        rows.append(
            artifact(
                "runbook:zzz-second",
                "runbook",
                "ZZZ Orchid operations",
                entities=("Orchid",),
                summary="Orchid backup runbook second target.",
            )
        )
    db = build_fixture_index(tmp_path, monkeypatch, rows)
    ids = [
        [row["id"] for row in index_owner.search_index(db, "orchid backup runbook", limit=size)]
        for size in (1, 5, 140)
    ]
    assert ids[0] == ids[2][:1]
    assert ids[1] == ids[2][:5]
    assert ids[0] == (["skill:baseline"] if ambiguous else ["runbook:zz-target"])


def test_search_uses_one_readonly_connection_and_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = build_fixture_index(tmp_path, monkeypatch, [
        artifact("skill:base", "skill", "Orchid backup"),
        artifact("runbook:target", "runbook", "Orchid operations", entities=("Orchid",)),
    ])
    original = index_owner.connect_readonly
    statements: list[str] = []
    connections = 0

    def counted(path: Path) -> sqlite3.Connection:
        nonlocal connections
        connections += 1
        connection = original(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(index_owner, "connect_readonly", counted)
    index_owner.search_index(db, "orchid backup runbook", limit=2)
    assert connections == 1
    assert [statement for statement in statements if statement.upper().startswith("BEGIN")] == ["BEGIN"]


def test_atomic_replacement_returns_one_coherent_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = build_fixture_index(tmp_path / "old", monkeypatch, [
        artifact("skill:old", "skill", "Orchid backup"),
        artifact("runbook:old", "runbook", "Orchid operations", entities=("Orchid",)),
    ])
    new = build_fixture_index(tmp_path / "new", monkeypatch, [
        artifact("skill:new", "skill", "Orchid backup"),
        artifact("runbook:new", "runbook", "Orchid operations", entities=("Orchid",)),
    ])
    original = index_owner._query_fts_rows
    replaced = False

    def replace_after_query(
        connection: sqlite3.Connection,
        match: str,
        candidate_limit: int,
        artifact_type: str,
    ) -> list[sqlite3.Row]:
        nonlocal replaced
        rows = original(connection, match, candidate_limit, artifact_type)
        if not replaced:
            replaced = True
            os.replace(new, old)
        return rows

    monkeypatch.setattr(index_owner, "_query_fts_rows", replace_after_query)
    assert [row["id"] for row in index_owner.search_index(old, "orchid backup runbook", limit=2)] == [
        "runbook:old",
        "skill:old",
    ]


def test_support_promotion_is_stable_and_preserves_diversity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchid, lotus = "skill:orchid", "skill:lotus"
    rows = [
        artifact("skill:baseline", "skill", "Alpha Orchid backup guide"),
        artifact(orchid, "skill", "Orchid router"),
        artifact(
            "skill-support:orchid:generic",
            "skill_support_doc",
            "Alpha Orchid backup appendix",
            related=(orchid,),
        ),
        artifact(
            "skill-support:orchid:target",
            "skill_support_doc",
            "Zulu Orchid backup reference",
            entities=("Orchid",),
            related=(orchid,),
        ),
        artifact(lotus, "skill", "Lotus router", summary="Orchid backup docs interoperability."),
        artifact(
            "skill-support:lotus:a",
            "skill_support_doc",
            "Lotus interoperability",
            related=(lotus,),
            summary="Orchid backup docs interoperability.",
        ),
        artifact(
            "skill-support:lotus:b",
            "skill_support_doc",
            "Lotus appendix",
            related=(lotus,),
            summary="Orchid backup docs interoperability.",
        ),
        artifact(
            "runbook:unrelated",
            "runbook",
            "Orchid backup notes",
            path="docs/orchid-backup-notes.md",
        ),
    ]
    db = build_fixture_index(tmp_path, monkeypatch, rows)
    assert [row["id"] for row in index_owner.search_index(db, "orchid backup docs", limit=8)] == [
        orchid,
        "skill-support:orchid:target",
        "runbook:unrelated",
        "skill:baseline",
        lotus,
        "skill-support:lotus:b",
    ]
