from __future__ import annotations

import inspect
import json
import multiprocessing
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from hermes_local_knowledge import index
from hermes_local_knowledge.artifacts import Artifact, Edge
from hermes_local_knowledge.config import IndexSettings


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def skill(name: str, *, related: str = "") -> str:
    relation = f"  related_skills: [{related}]\n" if related else ""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Route {name} inventory operations.\n"
        "metadata:\n"
        "  hermes:\n"
        f"{relation}"
        "---\n\n"
        f"# {name}\n"
    )


def settings() -> IndexSettings:
    return IndexSettings(
        custom_skill_dirs=("custom_skills",),
        script_dirs=("scripts",),
        memory_dirs=("memory",),
        runbook_dirs=("docs",),
        known_entities=("Quartz",),
        include_markdown_docs=True,
    )


def _build_in_process(
    root: str,
    state: str,
    hermes_home: str,
    started: Any | None,
    release: Any | None,
    ready: Any | None,
    collect_entered: Any | None,
    force: bool,
    results: Any,
) -> None:
    try:
        from hermes_local_knowledge import index as process_index
        from hermes_local_knowledge.config import IndexSettings as ProcessSettings

        if started is not None or collect_entered is not None:
            original_collect = process_index.collect_artifacts

            def controlled_collect(*args: Any, **kwargs: Any) -> list[Artifact]:
                if collect_entered is not None:
                    collect_entered.set()
                artifacts = original_collect(*args, **kwargs)
                if started is not None:
                    started.set()
                if release is not None and not release.wait(30):
                    raise TimeoutError("test release event was not set")
                return artifacts

            process_index.collect_artifacts = controlled_collect
        if ready is not None:
            ready.set()
        process_index.build_index(
            Path(root),
            Path(state),
            Path(hermes_home),
            ProcessSettings(
                custom_skill_dirs=("custom_skills",),
                script_dirs=("scripts",),
                memory_dirs=("memory",),
                runbook_dirs=("docs",),
                known_entities=("Quartz",),
                include_markdown_docs=True,
            ),
            force=force,
        )
        results.put(None)
    except BaseException as exc:  # pragma: no cover - surfaced in the parent assertion
        results.put(f"{type(exc).__name__}: {exc}")


def test_build_publishes_valid_format4_index_and_queries_it(tmp_path: Path) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "custom_skills" / "quartz-router" / "SKILL.md", skill("quartz-router", related="quartz-helper"))
    write(root / "custom_skills" / "quartz-helper" / "SKILL.md", skill("quartz-helper"))
    write(root / "docs" / "quartz-runbook.md", "# Quartz runbook\n\nInventory operations and recovery.\n")
    legacy_lock = state / index.LEGACY_INDEX_BUILD_LOCK_NAME
    write(legacy_lock, '{"pid": 123, "created_at": "legacy"}\n')

    build_result = index.build_index(root, state, hermes_home, settings())
    assert build_result is not None
    artifacts, edges = build_result

    assert {artifact.id for artifact in artifacts} >= {"skill:quartz-router", "skill:quartz-helper"}
    assert Edge(
        source="skill:quartz-helper",
        target="skill:quartz-router",
        kind="related_to",
        evidence="skill:quartz-helper",
    ) in edges or Edge(
        source="skill:quartz-router",
        target="skill:quartz-helper",
        kind="related_to",
        evidence="skill:quartz-helper",
    ) in edges
    db_path = state / "index.sqlite"
    assert index.index_format_state(db_path) == ("current", 4)
    assert index.index_needs_rebuild(db_path) is False
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        assert metadata["format_version"] == "4"
        assert metadata["plugin_version"] == "0.4.0"
        assert int(metadata["artifact_count"]) == len(artifacts)
        assert int(metadata["edge_count"]) == len(edges)
    assert legacy_lock.read_text(encoding="utf-8") == '{"pid": 123, "created_at": "legacy"}\n'
    with sqlite3.connect(state / index.INDEX_BUILD_LOCK_NAME) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    jsonl = [json.loads(line) for line in (state / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["id"] for row in jsonl} == {artifact.id for artifact in artifacts}
    assert all("search_text" not in row for row in jsonl)

    results = index.search_index(db_path, "quartz inventory operations", limit=3)
    assert results
    assert results[0]["id"].startswith("skill:quartz")
    assert set(results[0]) == {
        "id",
        "type",
        "title",
        "path",
        "summary",
        "triggers",
        "entities",
        "related",
        "updated_at",
        "source",
        "rank",
    }
    fetched = index.get_artifact(db_path, "skill:quartz-router")
    assert fetched is not None
    assert fetched["id"] == "skill:quartz-router"
    assert index.get_neighbors(db_path, "skill:quartz-router")


def test_missing_older_and_corrupt_indexes_rebuild_but_newer_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "docs" / "guide.md", "# Guide\n\nQuartz operations.\n")
    state.mkdir()
    db_path = state / "index.sqlite"
    db_path.write_bytes(b"not sqlite")
    assert index.index_format_state(db_path) == ("corrupt", None)
    index.build_index(root, state, hermes_home, settings())
    assert index.index_format_state(db_path) == ("current", 4)

    db_path.unlink()
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version=3")
    assert index.index_format_state(db_path) == ("older", 3)
    index.build_index(root, state, hermes_home, settings())
    assert index.index_format_state(db_path) == ("current", 4)

    db_path.unlink()
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version=4")
    assert index.index_format_state(db_path) == ("corrupt", 4)
    assert index.index_needs_rebuild(db_path) is True
    index.build_index(root, state, hermes_home, settings())
    assert index.index_format_state(db_path) == ("current", 4)

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE metadata SET value='-1' WHERE key='artifact_count'")
    assert index.index_format_state(db_path) == ("corrupt", 4)
    index.build_index(root, state, hermes_home, settings())
    assert index.index_format_state(db_path) == ("current", 4)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO artifact_fts (id, type, title, summary, triggers, entities, path, search_text)
            SELECT id, type, title, summary, triggers, entities, path, search_text
            FROM artifact_fts LIMIT 1
            """
        )
    assert index.index_format_state(db_path) == ("corrupt", 4)
    index.build_index(root, state, hermes_home, settings())
    assert index.index_format_state(db_path) == ("current", 4)

    db_path.unlink()
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version=5")

    def fail_if_scanned(*args: Any, **kwargs: Any) -> list[Artifact]:
        raise AssertionError("newer index must be rejected before scanning")

    monkeypatch.setattr(index, "collect_artifacts", fail_if_scanned)
    with pytest.raises(index.NewerIndexFormatError):
        index.build_index(root, state, hermes_home, settings())
    assert index.index_format_state(db_path) == ("newer", 5)


def test_current_index_requires_an_fts5_virtual_table(tmp_path: Path) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    empty_settings = IndexSettings(
        custom_skill_dirs=("custom_skills",),
        script_dirs=("scripts",),
        memory_dirs=("memory",),
        runbook_dirs=("docs",),
        include_markdown_docs=False,
    )
    index.build_index(root, state, hermes_home, empty_settings)
    db_path = state / "index.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE artifact_fts")
        connection.execute(
            """
            CREATE TABLE artifact_fts (
                id /* USING fts5( */, type, title, summary, triggers, entities, path, search_text
            )
            """
        )

    assert index.index_format_state(db_path) == ("corrupt", 4)
    assert index.index_needs_rebuild(db_path) is True


def test_current_index_rejects_null_artifact_ids(tmp_path: Path) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "docs" / "guide.md", "# Guide\n\nQuartz operations.\n")
    index.build_index(root, state, hermes_home, settings())
    db_path = state / "index.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE artifacts SET id=NULL")
        connection.execute("UPDATE artifact_fts SET id=NULL")

    assert index.index_format_state(db_path) == ("corrupt", 4)
    assert index.index_needs_rebuild(db_path) is True


def test_nonforced_build_rechecks_current_index_and_dirty_tokens_under_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "docs" / "guide.md", "# Guide\n\nQuartz operations.\n")
    index.build_index(root, state, hermes_home, settings())
    original_collect = index.collect_artifacts
    assert "acquire_lock" not in inspect.signature(index.build_index).parameters

    def fail_if_scanned(*args: Any, **kwargs: Any) -> list[Artifact]:
        raise AssertionError("current non-dirty index must not be scanned")

    monkeypatch.setattr(index, "collect_artifacts", fail_if_scanned)
    assert index.build_index(root, state, hermes_home, settings(), force=False) is None

    marker = state / index.DIRTY_MARKER_NAME
    marker.mkdir(parents=True)
    token = marker / "new-okf"
    token.touch()
    monkeypatch.setattr(index, "collect_artifacts", original_collect)
    assert index.build_index(root, state, hermes_home, settings(), force=False) is not None
    assert not token.exists()


def test_mcp_credentials_never_reach_persistence_or_public_results(tmp_path: Path) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(
        hermes_home / "config.yaml",
        """mcp_servers:
  credential-probe:
    command: uvx
    url: "https://user-canary:password-canary@example.invalid/api?token=query-canary&sig=signed-canary&client%5Fsecret=encoded-canary#client_secret=fragment-canary"
    args:
      - credential-probe
      - --token-file
      - /home/example/token-file-canary
      - --header
      - "Cookie: session=cookie-canary"
""",
    )

    build_result = index.build_index(root, state, hermes_home, settings())
    assert build_result is not None
    artifacts, _edges = build_result
    db_path = state / "index.sqlite"
    with sqlite3.connect(db_path) as connection:
        persisted = json.dumps(
            {
                "artifacts": connection.execute("SELECT * FROM artifacts").fetchall(),
                "fts": connection.execute("SELECT * FROM artifact_fts").fetchall(),
            }
        )
    public_payload = json.dumps(
        {
            "collected": [asdict(artifact) for artifact in artifacts],
            "jsonl": (state / "index.jsonl").read_text(encoding="utf-8"),
            "get": index.get_artifact(db_path, "mcp:credential-probe"),
            "search": index.search_index(db_path, "credential probe mcp"),
            "persisted": persisted,
        }
    ).lower()
    for canary in (
        "user-canary",
        "password-canary",
        "query-canary",
        "fragment-canary",
        "token-file-canary",
        "signed-canary",
        "encoded-canary",
        "cookie-canary",
    ):
        assert canary not in public_payload
    assert "token=<redacted>" in public_payload
    assert "client_secret=<redacted>" in public_payload
    assert "--token-file <redacted>" in public_payload
    assert "sig=<redacted>" in public_payload
    assert "client%5fsecret=<redacted>" in public_payload
    assert "cookie: <redacted>" in public_payload


def test_failed_build_preserves_prior_authoritative_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "docs" / "guide.md", "# Stable guide\n\nQuartz recovery.\n")
    index.build_index(root, state, hermes_home, settings())
    before = (state / "index.sqlite").read_bytes()
    assert index.search_index(state / "index.sqlite", "quartz recovery")

    def fail_collection(*args: Any, **kwargs: Any) -> list[Artifact]:
        raise RuntimeError("synthetic scan failure")

    monkeypatch.setattr(index, "collect_artifacts", fail_collection)
    with pytest.raises(RuntimeError, match="synthetic scan failure"):
        index.build_index(root, state, hermes_home, settings())

    assert (state / "index.sqlite").read_bytes() == before
    assert index.search_index(state / "index.sqlite", "quartz recovery")


def test_validation_failure_preserves_prior_index_dirty_token_and_cleans_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "docs" / "guide.md", "# Stable\n\nQuartz recovery.\n")
    index.build_index(root, state, hermes_home, settings())
    before_sqlite = (state / "index.sqlite").read_bytes()
    before_jsonl = (state / "index.jsonl").read_bytes()
    marker = state / index.DIRTY_MARKER_NAME
    marker.mkdir(parents=True)
    token = marker / "validation-failure"
    token.touch()
    original_validate = index._validate_sqlite

    def reject_candidate(*args: Any, **kwargs: Any) -> None:
        raise ValueError("synthetic candidate validation failure")

    monkeypatch.setattr(index, "_validate_sqlite", reject_candidate)
    with pytest.raises(ValueError, match="synthetic candidate validation failure"):
        index.build_index(root, state, hermes_home, settings())
    monkeypatch.setattr(index, "_validate_sqlite", original_validate)

    assert (state / "index.sqlite").read_bytes() == before_sqlite
    assert (state / "index.jsonl").read_bytes() == before_jsonl
    assert token.is_file()
    assert not list(state.glob(".*.tmp"))
    assert index.index_format_state(state / "index.sqlite") == ("current", 4)


def test_sqlite_replace_failure_keeps_prior_authority_and_dirty_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "docs" / "stable.md", "# Stable\n\nQuartz recovery.\n")
    index.build_index(root, state, hermes_home, settings())
    before_sqlite = (state / "index.sqlite").read_bytes()
    write(root / "docs" / "new.md", "# New\n\nReplacement boundary canary.\n")
    marker = state / index.DIRTY_MARKER_NAME
    marker.mkdir(parents=True)
    token = marker / "replace-failure"
    token.touch()

    def reject_replace(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("synthetic SQLite replacement failure")

    monkeypatch.setattr(index, "_replace_with_retry", reject_replace)
    with pytest.raises(PermissionError, match="synthetic SQLite replacement failure"):
        index.build_index(root, state, hermes_home, settings())

    assert (state / "index.sqlite").read_bytes() == before_sqlite
    assert token.is_file()
    assert not list(state.glob(".*.tmp"))
    assert not index.search_index(state / "index.sqlite", "replacement boundary canary")


def test_success_removes_only_dirty_tokens_covered_at_build_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "docs" / "guide.md", "# Guide\n\nQuartz operations.\n")
    marker = state / index.DIRTY_MARKER_NAME
    marker.mkdir(parents=True)
    covered = marker / "covered"
    covered.touch()
    original_collect = index.collect_artifacts

    def collect_and_mark(*args: Any, **kwargs: Any) -> list[Artifact]:
        artifacts = original_collect(*args, **kwargs)
        (marker / "arrived-during-build").touch()
        return artifacts

    monkeypatch.setattr(index, "collect_artifacts", collect_and_mark)
    index.build_index(root, state, hermes_home, settings())

    assert not covered.exists()
    assert (marker / "arrived-during-build").is_file()


def test_candidate_union_uses_one_ranker_then_parent_lifting_and_diversity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    artifacts = [
        Artifact(
            id="skill:alpha",
            type="skill",
            title="Alpha Router",
            path="custom_skills/alpha",
            summary="Route alpha operations.",
            triggers=["alpha operations"],
            search_text="alpha operations",
        ),
        Artifact(
            id="skill-support:alpha:one",
            type="skill_support_doc",
            title="Alpha needle one",
            path="custom_skills/alpha/references/one.md",
            summary="Needle phrase details.",
            related=["skill:alpha"],
            search_text="needle phrase alpha",
        ),
        Artifact(
            id="skill-support:alpha:two",
            type="skill_support_doc",
            title="Alpha needle two",
            path="custom_skills/alpha/references/two.md",
            summary="Needle phrase appendix.",
            related=["skill:alpha"],
            search_text="needle phrase alpha appendix",
        ),
        Artifact(
            id="script:alpha-wrapper",
            type="script",
            title="Alpha Wrapper",
            path="scripts/alpha_wrapper.py",
            summary="Run alpha inventory operations.",
            search_text="alpha inventory operations wrapper script",
        ),
        Artifact(
            id="runbook:alpha",
            type="runbook",
            title="Alpha inventory guide",
            path="docs/alpha.md",
            summary="Alpha inventory operations and wrapper guidance.",
            search_text="alpha inventory operations wrapper guidance",
        ),
        Artifact(
            id="doc:backup-strategy",
            type="doc",
            title="Recovery guidance",
            path="docs/recovery.md",
            summary="A genuine backup strategy for recovery.",
            search_text="genuine backup strategy recovery",
        ),
        Artifact(
            id="skill:mybackup-substrategy",
            type="skill",
            title="mybackup-substrategy",
            path="custom_skills/mybackup-substrategy",
            summary="Unrelated compact identity.",
            search_text="unrelated compact identity",
        ),
    ]

    monkeypatch.setattr(index, "collect_artifacts", lambda *args, **kwargs: artifacts)
    monkeypatch.setattr(index, "build_edges", lambda rows: [])
    index.build_index(root, state, hermes_home, settings())
    db_path = state / "index.sqlite"

    operational = index.search_index(db_path, "alpha inventory script", limit=5)
    assert operational[0]["id"] == "script:alpha-wrapper"
    support_only = index.search_index(db_path, "needle phrase", limit=5, artifact_type="skill_support_doc")
    assert support_only
    assert {row["type"] for row in support_only} == {"skill_support_doc"}
    parent_batches: list[tuple[str, ...]] = []
    fetch_parents = index._fetch_parents

    def counted_fetch_parents(connection: Any, artifact_ids: list[str]) -> dict[str, dict[str, Any]]:
        parent_batches.append(tuple(artifact_ids))
        return fetch_parents(connection, artifact_ids)

    monkeypatch.setattr(index, "_fetch_parents", counted_fetch_parents)
    lifted = index.search_index(db_path, "needle phrase", limit=5)
    assert lifted[0]["id"] == "skill:alpha"
    assert len([row for row in lifted if row["type"] == "skill_support_doc"]) == 1
    assert [batch for batch in parent_batches if batch] == [
        ("skill:alpha",),
        ("skill:alpha",),
    ]
    quoted = index.search_index(db_path, '"needle phrase"', limit=5)
    assert quoted
    assert quoted[0]["type"] == "skill_support_doc"
    assert all(row["id"] != "skill:alpha" for row in quoted)
    relevance = index.search_index(db_path, "backup strategy", limit=2)
    assert relevance[0]["id"] == "doc:backup-strategy"

    fts_calls: list[str] = []
    query_fts_rows = index._query_fts_rows

    def counted_query_fts_rows(*args: Any, **kwargs: Any) -> list[Any]:
        fts_calls.append(str(args[1]))
        return query_fts_rows(*args, **kwargs)

    def unexpected_metadata_rows(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("filled strict results must skip metadata fallback")

    monkeypatch.setattr(index, "_query_fts_rows", counted_query_fts_rows)
    monkeypatch.setattr(index, "_query_metadata_rows", unexpected_metadata_rows)
    strict_only = index.search_index(db_path, "needle phrase", limit=1)
    assert strict_only[0]["id"] == "skill:alpha"
    assert len(fts_calls) == 1


def test_waiting_builder_rebuilds_dirty_update_without_losing_token(tmp_path: Path) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "docs" / "initial.md", "# Initial\n\nQuartz baseline operations.\n")
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    release = context.Event()
    results = context.Queue()
    builder_a = context.Process(
        target=_build_in_process,
        args=(str(root), str(state), str(hermes_home), started, release, None, None, True, results),
    )
    builder_a.start()
    assert started.wait(30)

    write(root / "docs" / "concurrent.md", "# Concurrent update\n\nQuartz concurrent marker.\n")
    marker = state / index.DIRTY_MARKER_NAME
    marker.mkdir(parents=True, exist_ok=True)
    dirty_token = marker / "concurrent-update"
    dirty_token.touch()
    builder_b_ready = context.Event()
    builder_b_collect_entered = context.Event()
    builder_b = context.Process(
        target=_build_in_process,
        args=(
            str(root),
            str(state),
            str(hermes_home),
            None,
            None,
            builder_b_ready,
            builder_b_collect_entered,
            False,
            results,
        ),
    )
    builder_b.start()
    assert builder_b_ready.wait(30)
    assert not builder_b_collect_entered.wait(0.5)
    release.set()
    assert builder_b_collect_entered.wait(30)

    builder_a.join(30)
    builder_b.join(30)
    assert builder_a.exitcode == 0
    assert builder_b.exitcode == 0
    assert results.get(timeout=5) is None
    assert results.get(timeout=5) is None
    final = index.search_index(state / "index.sqlite", "quartz concurrent marker", limit=10)
    assert any("concurrent.md" in str(row["path"]) for row in final)
    assert not dirty_token.exists()


def test_waiting_nonforced_builder_rechecks_and_skips_after_token_consumed(tmp_path: Path) -> None:
    root = tmp_path / "source"
    state = tmp_path / "state"
    hermes_home = tmp_path / "home"
    write(root / "docs" / "initial.md", "# Initial\n\nQuartz baseline operations.\n")
    index.build_index(root, state, hermes_home, settings())
    marker = state / index.DIRTY_MARKER_NAME
    marker.mkdir(parents=True)
    dirty_token = marker / "covered-by-builder-a"
    dirty_token.touch()

    context = multiprocessing.get_context("spawn")
    builder_a_started = context.Event()
    builder_a_release = context.Event()
    builder_b_ready = context.Event()
    builder_b_collect_entered = context.Event()
    results = context.Queue()
    builder_a = context.Process(
        target=_build_in_process,
        args=(
            str(root),
            str(state),
            str(hermes_home),
            builder_a_started,
            builder_a_release,
            None,
            None,
            False,
            results,
        ),
    )
    builder_a.start()
    assert builder_a_started.wait(30)

    builder_b = context.Process(
        target=_build_in_process,
        args=(
            str(root),
            str(state),
            str(hermes_home),
            None,
            None,
            builder_b_ready,
            builder_b_collect_entered,
            False,
            results,
        ),
    )
    builder_b.start()
    assert builder_b_ready.wait(30)
    assert not builder_b_collect_entered.wait(0.5)
    builder_a_release.set()

    builder_a.join(30)
    builder_b.join(30)
    assert builder_a.exitcode == 0
    assert builder_b.exitcode == 0
    assert results.get(timeout=5) is None
    assert results.get(timeout=5) is None
    assert not builder_b_collect_entered.is_set()
    assert not dirty_token.exists()
