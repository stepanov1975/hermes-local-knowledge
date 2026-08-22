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


@pytest.mark.parametrize(
    ("noun", "expected_id"),
    [
        ("runbook", "runbook:orchid"),
        ("runbooks", "runbook:orchid"),
        ("skill", "skill:orchid-reference"),
        ("skills", "skill:orchid-reference"),
        ("doc", "doc:orchid"),
        ("docs", "doc:orchid"),
        ("document", "doc:orchid"),
        ("documentation", "doc:orchid"),
        ("reference", "doc:orchid"),
        ("references", "doc:orchid"),
        ("memory", "memory:orchid"),
        ("memories", "memory:orchid"),
    ],
)
def test_exact_artifact_nouns_promote_only_matching_service_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    noun: str,
    expected_id: str,
) -> None:
    all_nouns = (
        "runbook runbooks procedure procedures skill skills doc docs document documentation "
        "reference references memory memories"
    )
    artifacts = [
        Artifact(
            id="skill:orchid-reference",
            type="skill",
            title="Orchid reference",
            path="skills/orchid",
            summary=f"Orchid deployment service reference with {all_nouns}.",
            entities=["Orchid"],
            search_text=f"orchid deployment service reference {all_nouns}",
        ),
        Artifact(
            id="runbook:orchid",
            type="runbook",
            title="Orchid operations",
            path="docs/orchid-runbook.md",
            summary="Orchid deployment service runbook and procedure.",
            entities=["Orchid"],
            search_text="orchid deployment service runbook runbooks procedure procedures",
        ),
        Artifact(
            id="doc:orchid",
            type="doc",
            title="Orchid service documentation",
            path="docs/orchid-reference.md",
            summary="Orchid deployment service doc, document, documentation, and reference.",
            entities=["Orchid"],
            search_text="orchid deployment service doc docs document documentation reference references",
        ),
        Artifact(
            id="memory:orchid",
            type="memory_doc",
            title="Orchid memory",
            path="memory/orchid.md",
            summary="Orchid deployment service memory.",
            entities=["Orchid"],
            search_text="orchid deployment service memory memories",
        ),
        Artifact(
            id="runbook:lotus",
            type="runbook",
            title="Lotus runbook",
            path="docs/lotus.md",
            summary="Lotus service runbook only.",
            search_text="lotus service runbook runbooks procedure procedures",
        ),
    ]
    db_path = build_fixture_index(tmp_path, monkeypatch, artifacts)

    results = index_owner.search_index(db_path, f"orchid deployment {noun}", limit=5)

    assert results[0]["id"] == expected_id
    assert [row["id"] for row in results].index("skill:orchid-reference") < next(
        (index for index, row in enumerate(results) if row["id"] == "runbook:lotus"),
        len(results),
    )


def test_new_type_intent_requires_one_terminal_artifact_noun() -> None:
    assert index_owner._requested_operational_types(
        index_owner._query_terms("local skill installation example")
    ) == set()
    assert index_owner._requested_operational_types(
        index_owner._query_terms("local procedure")
    ) == set()
    assert index_owner._requested_operational_types(
        index_owner._query_terms("paperless cron script runbook")
    ) == {"cron_job", "script"}
    assert index_owner._requested_operational_types(
        index_owner._raw_query_terms("orchid runbook and skill")
    ) == set()
    assert index_owner._requested_operational_types(
        index_owner._raw_query_terms("orchid deployment runbook next")
    ) == set()
    automation_terms = index_owner._query_terms("automation workflow runbook")
    automation_intent = index_owner._operational_intent_terms(
        index_owner._raw_query_terms("automation workflow runbook")
    )
    assert index_owner._specific_terms_for_ranking(
        automation_terms,
        automation_intent,
        explicit_type_intent=True,
    ) == []
    assert index_owner._requested_operational_types(
        index_owner._raw_query_terms(
            "for the orchid runbook show the deployment runbook"
        )
    ) == {"runbook"}


@pytest.mark.parametrize(
    ("query", "artifact_type"),
    [
        ("cron", "cron_job"),
        ("job", "cron_job"),
        ("jobs", "cron_job"),
        ("mcp", "mcp_server"),
    ],
)
def test_legacy_type_only_intent_retains_operational_priority(
    query: str,
    artifact_type: str,
) -> None:
    requested = index_owner._requested_operational_types(index_owner._query_terms(query))
    operational = index_owner._Candidate(
        {"id": f"{artifact_type}:target", "type": artifact_type, "title": query},
        source_tier=0,
        strict=True,
    )
    guide = index_owner._Candidate(
        {"id": "skill:guide", "type": "skill", "title": query},
        source_tier=0,
        strict=True,
    )

    assert index_owner._operational_tier(operational, requested, []) < index_owner._operational_tier(
        guide,
        requested,
        [],
    )


def test_explicit_type_intent_promotes_one_full_match_without_crowding() -> None:
    owner = index_owner._Candidate(
        {"id": "skill:orchid", "type": "skill", "title": "Orchid deployment"},
        source_tier=0,
        strict=True,
    )
    best_runbook = index_owner._Candidate(
        {
            "id": "runbook:orchid",
            "type": "runbook",
            "title": "Orchid deployment",
            "entities": ["Orchid"],
        },
        source_tier=1,
        strict=False,
    )
    second_runbook = index_owner._Candidate(
        {"id": "runbook:orchid-2", "type": "runbook", "title": "Orchid deployment"},
        source_tier=2,
        strict=False,
    )

    promoted = index_owner._promote_explicit_type_candidate(
        [owner, best_runbook, second_runbook],
        {"runbook"},
        ["orchid", "deployment"],
    )

    assert [candidate.row["id"] for candidate in promoted] == [
        "runbook:orchid",
        "skill:orchid",
        "runbook:orchid-2",
    ]
    assert index_owner._promote_explicit_type_candidate(
        [owner, best_runbook],
        {"runbook"},
        ["orchid"],
    ) == [owner, best_runbook]
    assert index_owner._promote_explicit_type_candidate(
        [owner, best_runbook],
        {"runbook"},
        ["update", "app", "upgrade", "docker"],
    ) == [owner, best_runbook]
    assert index_owner._promote_explicit_type_candidate(
        [owner, best_runbook],
        {"runbook"},
        ["backup", "operation"],
    ) == [owner, best_runbook]
    topic_only_runbook = index_owner._Candidate(
        {
            "id": "runbook:generic",
            "type": "runbook",
            "title": "Generic operations guide",
            "summary": "Disaster recovery procedure",
        },
        source_tier=1,
        strict=False,
    )
    assert index_owner._promote_explicit_type_candidate(
        [owner, topic_only_runbook],
        {"runbook"},
        ["disaster", "recovery"],
    ) == [owner, topic_only_runbook]
    incident_runbook = index_owner._Candidate(
        {
            "id": "runbook:incident-response",
            "type": "runbook",
            "title": "Incident response runbook",
            "path": "docs/incident-response.md",
        },
        source_tier=1,
        strict=True,
    )
    assert index_owner._promote_explicit_type_candidate(
        [owner, incident_runbook],
        {"runbook"},
        ["incident", "response"],
    ) == [owner, incident_runbook]
    docker_runbooks = [
        index_owner._Candidate(
            {
                "id": f"runbook:docker-{name}",
                "type": "runbook",
                "title": f"Docker {name} update",
                "entities": ["Docker"],
            },
            source_tier=1,
            strict=True,
        )
        for name in ("alpha", "beta")
    ]
    assert index_owner._promote_explicit_type_candidate(
        [owner, *docker_runbooks],
        {"runbook"},
        ["docker", "update"],
    ) == [owner, *docker_runbooks]
    orchid_mixed = index_owner._Candidate(
        {
            "id": "runbook:orchid-mixed",
            "type": "runbook",
            "title": "Orchid deployment",
            "summary": "Lotus deployment interoperability.",
            "entities": ["Orchid"],
        },
        source_tier=1,
        strict=True,
    )
    lotus_mixed = index_owner._Candidate(
        {
            "id": "runbook:lotus-mixed",
            "type": "runbook",
            "title": "Lotus deployment",
            "summary": "Orchid deployment interoperability.",
            "entities": ["Lotus"],
        },
        source_tier=1,
        strict=True,
    )
    assert index_owner._promote_explicit_type_candidate(
        [owner, orchid_mixed, lotus_mixed],
        {"runbook"},
        ["orchid", "lotus", "deployment"],
    ) == [owner, orchid_mixed, lotus_mixed]
    redirection = index_owner._Candidate(
        {
            "id": "runbook:redirection",
            "type": "runbook",
            "title": "Redirection backup",
            "summary": "Redis backup compatibility.",
            "entities": ["Redirection"],
        },
        source_tier=1,
        strict=True,
    )
    redis = index_owner._Candidate(
        {
            "id": "runbook:redis",
            "type": "runbook",
            "title": "Redis backup",
            "summary": "Redis backup procedure.",
            "entities": ["Redis"],
        },
        source_tier=1,
        strict=True,
    )
    assert index_owner._promote_explicit_type_candidate(
        [owner, redirection, redis],
        {"runbook"},
        ["redi", "backup"],
    ) == [redis, owner, redirection]


def test_artifact_noun_intent_preserves_generic_quoted_filtered_reference_and_parent_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = [
        Artifact(
            id="skill:orchid",
            type="skill",
            title="Orchid router",
            path="skills/orchid",
            summary="Orchid runbook script and service docs.",
            search_text="orchid runbook script service docs",
        ),
        Artifact(
            id="skill-support:orchid:reference",
            type="skill_support_doc",
            title="Orchid service reference",
            path="skills/orchid/references/service.md",
            summary="Orchid service docs.",
            related=["skill:orchid"],
            search_text="orchid service docs",
        ),
        Artifact(
            id="skill-support:orchid:appendix",
            type="skill_support_doc",
            title="Orchid service appendix",
            path="skills/orchid/references/appendix.md",
            summary="Orchid service docs appendix.",
            related=["skill:orchid"],
            search_text="orchid service docs appendix",
        ),
        Artifact(
            id="runbook:orchid",
            type="runbook",
            title="Orchid runbook",
            path="docs/orchid.md",
            summary="Orchid runbook.",
            search_text="orchid runbook",
        ),
        Artifact(
            id="script:orchid",
            type="script",
            title="Orchid script",
            path="scripts/orchid.py",
            summary="Orchid script.",
            search_text="orchid script",
        ),
    ]
    db_path = build_fixture_index(tmp_path, monkeypatch, artifacts)

    assert index_owner.search_index(db_path, "runbook", limit=5)[0]["id"] == "runbook:orchid"
    underspecified = [
        row["id"] for row in index_owner.search_index(db_path, "orchid runbook", limit=5)
    ]
    with monkeypatch.context() as baseline_patch:
        baseline_patch.setattr(index_owner, "_requested_operational_types", lambda _terms: set())
        baseline_patch.setattr(index_owner, "_operational_intent_terms", lambda _terms: set())
        baseline = [
            row["id"] for row in index_owner.search_index(db_path, "orchid runbook", limit=5)
        ]
    assert underspecified == baseline
    assert index_owner.search_index(db_path, '"orchid runbook"', limit=5)[0]["id"] == "runbook:orchid"
    assert {
        row["type"]
        for row in index_owner.search_index(
            db_path,
            "orchid docs",
            limit=5,
            artifact_type="skill_support_doc",
        )
    } == {"skill_support_doc"}
    docs = index_owner.search_index(db_path, "orchid docs", limit=5)
    baseline_docs = index_owner.search_index(
        db_path,
        "orchid docs",
        limit=5,
        _disable_explicit_intent=True,
    )
    assert [row["id"] for row in docs] == [row["id"] for row in baseline_docs]
    assert len([row for row in docs if row["type"] == "skill_support_doc"]) == 1
    specific_docs = index_owner.search_index(db_path, "orchid service docs", limit=5)
    baseline_specific_docs = index_owner.search_index(
        db_path,
        "orchid service docs",
        limit=5,
        _disable_explicit_intent=True,
    )
    assert [row["id"] for row in specific_docs] == [
        row["id"] for row in baseline_specific_docs
    ]
    assert len([row for row in specific_docs if row["type"] == "skill_support_doc"]) == 1
    script = index_owner.search_index(db_path, "orchid script", limit=5)
    baseline_script = index_owner.search_index(
        db_path,
        "orchid script",
        limit=5,
        _disable_explicit_intent=True,
    )
    assert [row["id"] for row in script] == [row["id"] for row in baseline_script]


def test_explicit_doc_promotion_selects_eligible_support_sibling_before_diversity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = [
        Artifact(
            id="skill:orchid",
            type="skill",
            title="Orchid router",
            path="skills/orchid",
            summary="Orchid deployment documentation.",
            entities=["Orchid"],
            search_text="orchid deployment docs",
        ),
        Artifact(
            id="skill-support:orchid:generic",
            type="skill_support_doc",
            title="Orchid deployment docs",
            path="skills/orchid/references/generic.md",
            summary="General deployment documentation.",
            related=["skill:orchid"],
            search_text="orchid deployment docs",
        ),
        Artifact(
            id="skill-support:orchid:reference",
            type="skill_support_doc",
            title="Orchid reference",
            path="skills/orchid/references/deployment.md",
            summary="Deployment docs.",
            entities=["Orchid"],
            related=["skill:orchid"],
            search_text="orchid deployment docs",
        ),
    ]
    db_path = build_fixture_index(tmp_path, monkeypatch, artifacts)

    results = index_owner.search_index(db_path, "orchid deployment docs", limit=5)

    assert [row["id"] for row in results[:2]] == [
        "skill:orchid",
        "skill-support:orchid:reference",
    ]
    assert "skill-support:orchid:generic" not in {row["id"] for row in results}

    filtered = index_owner.search_index(
        db_path,
        "orchid deployment docs",
        limit=4,
        artifact_type="skill_support_doc",
    )
    assert filtered[0]["id"] == "skill-support:orchid:generic"


def test_explicit_intent_without_promotion_uses_baseline_diversity() -> None:
    owner = index_owner._Candidate(
        {"id": "skill:orchid", "type": "skill", "title": "Orchid"},
        source_tier=1,
        strict=True,
    )
    baseline_support = index_owner._Candidate(
        {
            "id": "skill-support:orchid:baseline",
            "type": "skill_support_doc",
            "title": "Alpha baseline",
            "related": ["skill:orchid"],
        },
        source_tier=1,
        strict=True,
    )
    alternate_support = index_owner._Candidate(
        {
            "id": "skill-support:orchid:alternate",
            "type": "skill_support_doc",
            "title": "Alpha alternate",
            "related": ["skill:orchid"],
        },
        source_tier=0,
        strict=False,
    )

    rows = index_owner._finalize_candidates(
        [owner, alternate_support, baseline_support],
        {"skill"},
        ["orchid", "alpha"],
        True,
        5,
        baseline_output=[owner.row, baseline_support.row],
    )

    assert [row["id"] for row in rows] == [
        "skill:orchid",
        "skill-support:orchid:baseline",
    ]


def test_explicit_intent_without_eligible_target_reuses_full_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = [
        Artifact(
            id="skill:orchid",
            type="skill",
            title="Orchid skill",
            path="skills/orchid",
            summary="Orchid alpha behavior.",
            search_text="orchid alpha skill",
        ),
        Artifact(
            id="skill-support:orchid:a",
            type="skill_support_doc",
            title="Orchid alpha A",
            path="skills/orchid/references/a.md",
            summary="Orchid alpha skill details.",
            related=["skill:orchid"],
            search_text="orchid alpha skill",
        ),
        Artifact(
            id="skill-support:orchid:b",
            type="skill_support_doc",
            title="Orchid alpha B",
            path="skills/orchid/references/b.md",
            summary="Orchid alpha skill details.",
            related=["skill:orchid"],
            search_text="orchid alpha skill",
        ),
    ]
    db_path = build_fixture_index(tmp_path, monkeypatch, artifacts)

    baseline = index_owner.search_index(
        db_path,
        "orchid alpha skill",
        limit=5,
        _disable_explicit_intent=True,
    )
    explicit = index_owner.search_index(db_path, "orchid alpha skill", limit=5)

    assert [row["id"] for row in explicit] == [row["id"] for row in baseline]
