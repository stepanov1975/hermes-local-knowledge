from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_local_knowledge import cli as lci_cli
from hermes_local_knowledge import okf


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def configure_hermes_home(tmp_path: Path) -> tuple[Path, Path]:
    hermes_home = tmp_path / "hermes_home"
    state_dir = tmp_path / "state"
    source_root = tmp_path / "repo"
    source_root.mkdir()
    hermes_home.mkdir()
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {source_root}
  state_dir: {state_dir}
  okf:
    enabled: true
    auto_generate: false
    max_candidates_per_session: 2
    min_use_count: 1
""",
    )
    return hermes_home, state_dir


def load_stdout_json(capsys) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return json.loads(capsys.readouterr().out)


def seed_candidate(state_dir: Path, *, tool_name: str = "mcp__paperless__paperless_find_latest_document") -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "description": "Search customer OCR document text about divorce settlement and medical diagnosis for alice@example.com with token=abc123",
        "properties": {
            "query": {
                "type": "string",
                "default": "alice@example.com",
                "examples": ["sk-secret-value"],
            }
        },
        "required": ["query"],
    }
    okf.upsert_tool_candidate(
        state_dir,
        tool_name=tool_name,
        toolset="paperless",
        schema=schema,
        args={"query": "private document token=secret"},
    )
    return schema


def okf_markdown(tool_name: str, schema_hash: str) -> str:
    return f"""---
artifact_type: tool_okf
tool: {tool_name}
toolset: paperless
schema_hash: {schema_hash}
aliases:
  - latest paperless document metadata
triggers:
  - User asks for newest matching Paperless document metadata.
---

# Tool OKF: {tool_name}

Use this when routing requests for latest Paperless document metadata.
"""


def assert_no_private_schema_values(payload: object) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert "alice@example.com" not in rendered
    assert "token=abc123" not in rendered
    assert "sk-secret-value" not in rendered
    assert "divorce settlement" not in rendered


def test_okf_cli_claim_validate_and_complete_from_hermes_config(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home, state_dir = configure_hermes_home(tmp_path)
    schema = seed_candidate(state_dir)
    tool_name = "mcp__paperless__paperless_find_latest_document"

    status = lci_cli.main(
        [
            "okf",
            "claim",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--limit",
            "1",
            "--claim-token",
            "claim-1",
            "--json",
        ]
    )
    payload = load_stdout_json(capsys)

    assert status == 0
    assert payload["success"] is True
    assert payload["claim_token"] == "claim-1"
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    candidate = candidates[0]
    assert candidate["tool"] == tool_name
    assert candidate["schema_hash"] == okf.schema_hash(schema)
    assert "private document" not in json.dumps(candidate)
    assert_no_private_schema_values(payload)
    target_path = Path(str(candidate["target_path"]))
    assert target_path == okf.okf_file_path(state_dir, tool_name)

    write(target_path, okf_markdown(tool_name, str(candidate["schema_hash"])))

    validate_status = lci_cli.main(
        [
            "okf",
            "validate",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--claim-token",
            "claim-1",
            "--path",
            str(target_path),
            "--json",
        ]
    )
    validate_payload = load_stdout_json(capsys)
    assert validate_status == 0
    assert validate_payload["valid"] is True

    complete_status = lci_cli.main(
        [
            "okf",
            "complete",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--claim-token",
            "claim-1",
            "--tool",
            tool_name,
            "--path",
            str(target_path),
            "--json",
        ]
    )
    complete_payload = load_stdout_json(capsys)

    assert complete_status == 0
    assert complete_payload["success"] is True
    assert okf.queue_counts(state_dir) == {"done": 1}


def test_okf_cli_validate_rejects_wrong_path_and_secret_assignment(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home, state_dir = configure_hermes_home(tmp_path)
    seed_candidate(state_dir)
    claimed = okf.claim_candidates(state_dir, limit=1, claim_token="claim-2")
    tool_name = str(claimed[0]["tool_name"])
    bad_path = tmp_path / "outside.md"
    write(bad_path, okf_markdown(tool_name, str(claimed[0]["schema_hash"])) + "\napi_key=secret\n")

    status = lci_cli.main(
        [
            "okf",
            "validate",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--claim-token",
            "claim-2",
            "--path",
            str(bad_path),
            "--json",
        ]
    )
    payload = load_stdout_json(capsys)

    assert status == 1
    assert payload["valid"] is False
    error_list = payload["errors"]
    assert isinstance(error_list, list)
    errors = "\n".join(str(error) for error in error_list)
    assert "path must be under" in errors
    assert "secret-like" in errors


def test_okf_cli_validate_rejects_trivial_routing_phrase(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home, state_dir = configure_hermes_home(tmp_path)
    seed_candidate(state_dir)
    claimed = okf.claim_candidates(state_dir, limit=1, claim_token="claim-trivial")
    tool_name = str(claimed[0]["tool_name"])
    target_path = okf.okf_file_path(state_dir, tool_name)
    write(
        target_path,
        f"""---
artifact_type: tool_okf
tool: {tool_name}
schema_hash: {claimed[0]["schema_hash"]}
aliases:
  - x
---

# Tool OKF: {tool_name}
""",
    )

    status = lci_cli.main(
        [
            "okf",
            "validate",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--claim-token",
            "claim-trivial",
            "--path",
            str(target_path),
            "--json",
        ]
    )
    payload = load_stdout_json(capsys)

    assert status == 1
    assert payload["valid"] is False
    error_list = payload["errors"]
    assert isinstance(error_list, list)
    assert any("specific multi-word routing phrase" in str(error) for error in error_list)


def test_okf_cli_fail_requeues_until_max_attempts(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home, state_dir = configure_hermes_home(tmp_path)
    seed_candidate(state_dir, tool_name="knowledge_search")
    okf.claim_candidates(state_dir, limit=1, claim_token="claim-3")

    status = lci_cli.main(
        [
            "okf",
            "fail",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--claim-token",
            "claim-3",
            "--tool",
            "knowledge_search",
            "--error",
            "document text token=secret should not persist",
            "--json",
        ]
    )
    payload = load_stdout_json(capsys)

    assert status == 0
    assert payload["success"] is True
    assert okf.queue_counts(state_dir) == {"pending": 1}
    assert "secret" not in repr(okf.pending_candidates(state_dir, limit=1))


def test_okf_cli_status(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home, state_dir = configure_hermes_home(tmp_path)
    seed_candidate(state_dir)

    status = lci_cli.main(
        [
            "okf",
            "status",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--json",
        ]
    )
    payload = load_stdout_json(capsys)
    assert status == 0
    assert payload["counts"] == {"pending": 1}
    assert_no_private_schema_values(payload)
    pending = payload["pending"]
    assert isinstance(pending, list)
    assert len(pending) == 1


def test_okf_cli_claim_stops_reclaiming_stale_rows_at_attempt_cap(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home, state_dir = configure_hermes_home(tmp_path)
    tool_name = "mcp__paperless__paperless_find_latest_document"
    seed_candidate(state_dir, tool_name=tool_name)

    for attempt in range(okf.DEFAULT_MAX_ATTEMPTS):
        status = lci_cli.main(
            [
                "okf",
                "claim",
                "--from-hermes-config",
                "--hermes-home",
                str(hermes_home),
                "--claim-token",
                f"claim-{attempt}",
                "--json",
            ]
        )
        payload = load_stdout_json(capsys)
        assert status == 0
        assert payload["count"] == 1
        with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
            conn.execute(
                "UPDATE okf_candidates SET claimed_at = ? WHERE tool_name = ?",
                ("2000-01-01T00:00:00Z", tool_name),
            )

    status = lci_cli.main(
        [
            "okf",
            "claim",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--claim-token",
            "claim-over-cap",
            "--json",
        ]
    )
    payload = load_stdout_json(capsys)

    assert status == 0
    assert payload["count"] == 0
    assert okf.queue_counts(state_dir) == {"error": 1}


def test_okf_cli_status_and_retry_exhausted_candidate(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home, state_dir = configure_hermes_home(tmp_path)
    tool_name = "mcp__siyuan__get_block_kramdown"
    seed_candidate(state_dir, tool_name=tool_name)

    for attempt in range(okf.DEFAULT_MAX_ATTEMPTS):
        claim_token = f"failed-claim-{attempt}"
        claimed = okf.claim_candidates(state_dir, limit=1, claim_token=claim_token)
        assert [row["tool_name"] for row in claimed] == [tool_name]
        assert okf.mark_candidate_error(
            state_dir,
            tool_name=tool_name,
            claim_token=claim_token,
            error="malformed generated OKF",
        )

    with sqlite3.connect(okf.okf_queue_db_path(state_dir)) as conn:
        conn.execute(
            """
            UPDATE okf_candidates
            SET claimed_at = ?, claim_token = ?, okf_path = ?
            WHERE tool_name = ?
            """,
            ("2026-07-10T12:00:00Z", "stale-claim", "/tmp/stale-invalid.md", tool_name),
        )
    before_retry = okf.error_candidates(state_dir, limit=1)[0]

    status = lci_cli.main(
        [
            "okf",
            "status",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--json",
        ]
    )
    status_payload = load_stdout_json(capsys)
    assert status == 0
    assert status_payload["counts"] == {"error": 1}
    errors = status_payload["errors"]
    assert isinstance(errors, list)
    assert [row["tool"] for row in errors] == [tool_name]
    assert_no_private_schema_values(status_payload)

    retry_status = lci_cli.main(
        [
            "okf",
            "retry",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--tool",
            tool_name,
            "--json",
        ]
    )
    retry_payload = load_stdout_json(capsys)

    assert retry_status == 0
    assert retry_payload == {"success": True, "tool": tool_name}
    assert okf.queue_counts(state_dir) == {"pending": 1}
    pending = okf.pending_candidates(state_dir, limit=1)
    assert pending[0]["attempt_count"] == 0
    assert pending[0]["claimed_at"] is None
    assert pending[0]["claim_token"] is None
    assert pending[0]["okf_path"] is None
    assert pending[0]["last_attempt_error"] is None
    for preserved_field in (
        "tool_name",
        "toolset",
        "schema_hash",
        "schema_json",
        "use_count",
        "success_count",
        "error_count",
        "last_error_type",
        "last_error_message",
        "arg_shape_json",
    ):
        assert pending[0][preserved_field] == before_retry[preserved_field]
    reclaimed = okf.claim_candidates(state_dir, limit=1, claim_token="retry-claim")
    assert [row["tool_name"] for row in reclaimed] == [tool_name]


def test_okf_cli_retry_migrates_legacy_queue_schema(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    state_dir = tmp_path / "legacy-state"
    state_dir.mkdir()
    queue_db = okf.okf_queue_db_path(state_dir)
    tool_name = "legacy_tool"
    with sqlite3.connect(queue_db) as conn:
        conn.execute(
            """
            CREATE TABLE okf_candidates (
              tool_name TEXT PRIMARY KEY, toolset TEXT, schema_hash TEXT,
              schema_json TEXT, first_seen TEXT, last_seen TEXT,
              use_count INTEGER, success_count INTEGER, error_count INTEGER,
              last_error_type TEXT, last_error_message TEXT,
              arg_shape_json TEXT, status TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO okf_candidates VALUES (
              ?, 'legacy', 'sha256:legacy', '{}',
              '2026-07-01T00:00:00Z', '2026-07-02T00:00:00Z',
              7, 5, 2, 'tool_error', '<redacted>', '{"query":"str"}', 'error'
            )
            """,
            (tool_name,),
        )

    status = lci_cli.main(
        [
            "okf",
            "retry",
            "--state-dir",
            str(state_dir),
            "--tool",
            tool_name,
            "--json",
        ]
    )
    payload = load_stdout_json(capsys)

    assert status == 0
    assert payload == {"success": True, "tool": tool_name}
    with sqlite3.connect(queue_db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(okf_candidates)")}
        row = conn.execute(
            """
            SELECT status, attempt_count, last_attempt_error, toolset,
                   schema_hash, use_count, success_count, error_count
            FROM okf_candidates WHERE tool_name = ?
            """,
            (tool_name,),
        ).fetchone()
    assert {"attempt_count", "last_attempt_error", "claim_token"} <= columns
    assert row == ("pending", 0, None, "legacy", "sha256:legacy", 7, 5, 2)
    claimed = okf.claim_candidates(state_dir, limit=1, claim_token="legacy-retry")
    assert [candidate["tool_name"] for candidate in claimed] == [tool_name]


def test_retry_error_candidate_does_not_recreate_removed_queue(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state_dir = tmp_path / "state"
    seed_candidate(state_dir, tool_name="race_tool")
    queue_db = okf.okf_queue_db_path(state_dir)
    with sqlite3.connect(queue_db) as conn:
        conn.execute("UPDATE okf_candidates SET status = 'error'")
    original_connect = okf.sqlite3.connect

    def remove_before_open(database, *args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("uri"):
            queue_db.unlink()
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(okf.sqlite3, "connect", remove_before_open)

    assert not okf.retry_error_candidate(state_dir, tool_name="race_tool")
    assert not queue_db.exists()


def test_retry_error_candidate_rolls_back_migration_if_row_disappears(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    queue_db = okf.okf_queue_db_path(state_dir)
    with sqlite3.connect(queue_db) as conn:
        conn.execute(
            "CREATE TABLE okf_candidates (tool_name TEXT PRIMARY KEY, status TEXT)"
        )
        conn.execute("INSERT INTO okf_candidates VALUES ('race_tool', 'error')")
    before_bytes = queue_db.read_bytes()
    original_ensure_schema = okf._ensure_schema

    def delete_before_migration(conn, *, commit=True):  # type: ignore[no-untyped-def]
        conn.execute("DELETE FROM okf_candidates WHERE tool_name = 'race_tool'")
        original_ensure_schema(conn, commit=commit)

    monkeypatch.setattr(okf, "_ensure_schema", delete_before_migration)

    assert not okf.retry_error_candidate(state_dir, tool_name="race_tool")
    assert queue_db.read_bytes() == before_bytes
    with sqlite3.connect(queue_db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(okf_candidates)")}
        rows = conn.execute("SELECT tool_name, status FROM okf_candidates").fetchall()
    assert columns == {"tool_name", "status"}
    assert rows == [("race_tool", "error")]


def test_retry_error_candidate_rejects_duplicate_malformed_schema_without_mutation(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    queue_db = okf.okf_queue_db_path(state_dir)
    with sqlite3.connect(queue_db) as conn:
        conn.execute("CREATE TABLE okf_candidates (tool_name TEXT, status TEXT)")
        conn.executemany(
            "INSERT INTO okf_candidates VALUES (?, 'error')",
            [("duplicate",), ("duplicate",)],
        )
    before_bytes = queue_db.read_bytes()

    with pytest.raises(RuntimeError, match="required tool_name primary key"):
        okf.retry_error_candidate(state_dir, tool_name="duplicate")

    assert queue_db.read_bytes() == before_bytes
    with sqlite3.connect(queue_db) as conn:
        rows = conn.execute("SELECT tool_name, status FROM okf_candidates").fetchall()
    assert rows == [("duplicate", "error"), ("duplicate", "error")]


def test_retry_missing_existing_candidate_does_not_mutate_queue(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    seed_candidate(state_dir, tool_name="other_tool")
    queue_db = okf.okf_queue_db_path(state_dir)
    before_bytes = queue_db.read_bytes()
    before_mtime_ns = queue_db.stat().st_mtime_ns

    assert not okf.retry_error_candidate(state_dir, tool_name="missing_tool")
    assert queue_db.read_bytes() == before_bytes
    assert queue_db.stat().st_mtime_ns == before_mtime_ns


def test_okf_cli_retry_rejects_non_error_candidate(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home, state_dir = configure_hermes_home(tmp_path)
    seed_candidate(state_dir, tool_name="knowledge_search")
    queue_db = okf.okf_queue_db_path(state_dir)
    before_bytes = queue_db.read_bytes()
    before_mtime_ns = queue_db.stat().st_mtime_ns

    status = lci_cli.main(
        [
            "okf",
            "retry",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
            "--tool",
            "knowledge_search",
            "--json",
        ]
    )
    payload = load_stdout_json(capsys)

    assert status == 1
    assert payload["success"] is False
    assert payload["errors"] == ["candidate is missing or not in terminal error state"]
    assert queue_db.read_bytes() == before_bytes
    assert queue_db.stat().st_mtime_ns == before_mtime_ns
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_okf_cli_retry_missing_candidate_does_not_create_state(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    state_dir = tmp_path / "absent-state"

    status = lci_cli.main(
        [
            "okf",
            "retry",
            "--state-dir",
            str(state_dir),
            "--tool",
            "missing_tool",
            "--json",
        ]
    )
    payload = load_stdout_json(capsys)

    assert status == 1
    assert payload["success"] is False
    assert not state_dir.exists()
