from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import hermes_local_knowledge
import pytest
from hermes_local_knowledge import cli as lci_cli
from hermes_local_knowledge import handlers as lci_handlers
from hermes_local_knowledge import okf
from hermes_local_knowledge import plugin
from hermes_local_knowledge import runtime as lci_runtime
from hermes_local_knowledge import storage as lci_storage
from hermes_local_knowledge import telemetry as lci_telemetry


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_version_metadata_stays_in_sync():
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    plugin_version = next(
        line.partition(":")[2].strip()
        for line in (repo_root / "plugin.yaml").read_text(encoding="utf-8").splitlines()
        if line.startswith("version:")
    )

    assert hermes_local_knowledge.__version__ == pyproject["project"]["version"]
    assert hermes_local_knowledge.__version__ == plugin_version


def test_packaging_discovery_excludes_mutation_workspace():
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    find_config = pyproject["tool"]["setuptools"]["packages"]["find"]
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert find_config["include"] == ["hermes_local_knowledge*"]
    assert "mutants*" in find_config["exclude"]
    assert package_data["hermes_local_knowledge"] == ["skills/*/SKILL.md"]


def make_temp_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    state_dir = tmp_path / "state"
    (repo / "scripts").mkdir(parents=True)
    (repo / "custom_skills" / "note-taking" / "paperless-review-automation").mkdir(parents=True)
    (repo / "custom_skills" / "note-taking" / "paperless-helper").mkdir(parents=True)
    hermes_home.mkdir()

    write(
        repo / "scripts" / "paperless_review_helper.py",
        """#!/usr/bin/env python3
\"\"\"Paperless review helper script for visual review automation.\"\"\"
""",
    )
    write(
        repo / "custom_skills" / "note-taking" / "paperless-review-automation" / "SKILL.md",
        """---
name: paperless-review-automation
description: Operate Paperless visual review automation and reviewer cron.
tags:
  - Paperless
  - review
related_skills:
  - paperless-helper
---
# Paperless review automation
""",
    )
    write(
        repo / "custom_skills" / "note-taking" / "paperless-helper" / "SKILL.md",
        """---
name: paperless-helper
description: Supporting Paperless helper procedures.
tags:
  - Paperless
---
# Paperless helper
""",
    )
    return repo, hermes_home, state_dir


def configure_env(monkeypatch, repo: Path, hermes_home: Path, state_dir: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LOCAL_KNOWLEDGE_ROOT", str(repo))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


def test_register_exposes_native_tools_and_bundled_skill():
    tool_calls = []
    skill_calls = []
    cli_calls = []

    class Ctx:
        def register_tool(self, **kwargs):
            tool_calls.append(kwargs)

        def register_skill(self, name, skill_md):  # type: ignore[no-untyped-def]
            skill_calls.append((name, Path(skill_md)))

        def register_cli_command(self, **kwargs):  # type: ignore[no-untyped-def]
            cli_calls.append(kwargs)

    plugin.register(Ctx())

    assert [call["name"] for call in tool_calls] == [
        "knowledge_search",
        "knowledge_get",
        "knowledge_neighbors",
        "knowledge_feedback",
        "knowledge_usage_report",
    ]
    assert {call["toolset"] for call in tool_calls} == {"local_knowledge"}
    assert all(call["schema"]["parameters"]["type"] == "object" for call in tool_calls)
    assert all(call["check_fn"] is plugin.check_knowledge_available for call in tool_calls)
    expected_skill = Path(__file__).resolve().parents[1] / "skills" / "local-knowledge-router" / "SKILL.md"
    assert skill_calls == [("local-knowledge-router", expected_skill)]
    assert skill_calls[0][1].is_file()
    assert len(cli_calls) == 1
    assert cli_calls[0]["name"] == "local-knowledge"
    assert callable(cli_calls[0]["setup_fn"])
    assert callable(cli_calls[0]["handler_fn"])


def test_bundled_router_skill_matches_install_example() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    bundled = repo_root / "skills" / "local-knowledge-router" / "SKILL.md"
    packaged = repo_root / "hermes_local_knowledge" / "skills" / "local-knowledge-router" / "SKILL.md"
    example = repo_root / "examples" / "local-knowledge-router-skill" / "SKILL.md"

    assert bundled.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")
    assert packaged.read_text(encoding="utf-8") == bundled.read_text(encoding="utf-8")


def test_plugin_handlers_honor_compatibility_module_monkeypatches(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    fake_root = Path("/tmp/fake-local-knowledge-root")

    def fake_repo_root() -> Path:
        calls.append("repo_root")
        return fake_root

    def fake_ensure_index(root: Path, *, rebuild: bool = False):  # type: ignore[no-untyped-def]
        calls.append(f"ensure:{root}:{rebuild}")
        raise RuntimeError("sentinel wrapper patch used")

    monkeypatch.setattr(plugin, "_repo_root", fake_repo_root)
    monkeypatch.setattr(plugin, "_ensure_index", fake_ensure_index)
    monkeypatch.setattr(plugin, "_record_usage", lambda *args, **kwargs: None)

    payload = json.loads(plugin._handle_search({"query": "demo", "rebuild": True}))

    assert calls == ["repo_root", f"ensure:{fake_root}:True"]
    assert payload["success"] is False
    assert "sentinel wrapper patch used" in payload["error"]


def test_plugin_rebuild_uses_compatibility_index_module(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    calls: list[str] = []

    class FakeIndex:
        def build_index(self, root: Path, output_dir: Path, home: Path, settings=None):  # type: ignore[no-untyped-def]
            calls.append(f"build:{root}:{output_dir}:{home}:{settings is not None}")
            return [], []

        def search_index(self, db_path: Path, query: str, limit: int = 8, artifact_type=None):  # type: ignore[no-untyped-def]
            calls.append(f"search:{db_path}:{query}:{limit}:{artifact_type}")
            return []

    monkeypatch.setattr(plugin, "_index_module", lambda _root: FakeIndex())

    payload = json.loads(plugin._handle_search({"query": "demo", "rebuild": True}))

    assert payload["success"] is True
    assert payload["rebuilt"] is True
    assert calls == [
        f"build:{repo.resolve()}:{state_dir.resolve()}:{hermes_home.resolve()}:True",
        f"search:{state_dir.resolve() / 'index.sqlite'}:demo:8:None",
    ]


def test_ensure_index_rebuilds_and_clears_okf_dirty_marker(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "index.sqlite").touch()
    okf.mark_index_dirty(state_dir)
    calls: list[str] = []

    def fake_build(root: Path, output_dir: Path, home: Path, settings):  # type: ignore[no-untyped-def]
        calls.append(f"build:{root}:{output_dir}:{home}:{settings is not None}")
        return [], []

    db_path, metadata = lci_runtime._ensure_index(repo, build_index_fn=fake_build)

    assert db_path == state_dir.resolve() / "index.sqlite"
    assert metadata["rebuilt"] is True
    assert calls == [f"build:{repo.resolve()}:{state_dir.resolve()}:{hermes_home.resolve()}:True"]
    assert not okf.index_dirty_tokens(state_dir)


def test_ensure_index_preserves_new_dirty_token_created_during_build(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "index.sqlite").touch()
    okf.mark_index_dirty(state_dir)

    def fake_build(root: Path, output_dir: Path, home: Path, settings):  # type: ignore[no-untyped-def]
        okf.mark_index_dirty(state_dir)
        return [], []

    lci_runtime._ensure_index(repo, build_index_fn=fake_build)

    assert len(okf.index_dirty_tokens(state_dir)) == 1


def test_ensure_index_preserves_dirty_tokens_when_build_fails(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "index.sqlite").touch()
    okf.mark_index_dirty(state_dir)

    def failing_build(root: Path, output_dir: Path, home: Path, settings):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated build failure")

    try:
        lci_runtime._ensure_index(repo, build_index_fn=failing_build)
    except RuntimeError as exc:
        assert str(exc) == "simulated build failure"
    else:
        raise AssertionError("expected build failure")

    assert len(okf.index_dirty_tokens(state_dir)) == 1
    assert lci_storage.index_build_lock_path(state_dir).exists()
    with lci_storage.index_build_lock(state_dir):
        pass


def test_ensure_index_serializes_concurrent_dirty_rebuilds(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "index.sqlite"
    db_path.write_text("initial", encoding="utf-8")
    okf.mark_index_dirty(state_dir)
    a_started = threading.Event()
    b_started = threading.Event()
    errors: list[BaseException] = []

    def coordinated_build(root: Path, output_dir: Path, home: Path, settings):  # type: ignore[no-untyped-def]
        if threading.current_thread().name == "index-build-a":
            a_started.set()
            b_started.wait(timeout=0.2)
            db_path.write_text("stale-without-new-okf", encoding="utf-8")
        else:
            b_started.set()
            db_path.write_text("fresh-with-new-okf", encoding="utf-8")
        return [], []

    def ensure_index() -> None:
        try:
            lci_runtime._ensure_index(repo, build_index_fn=coordinated_build)
        except BaseException as exc:
            errors.append(exc)

    thread_a = threading.Thread(target=ensure_index, name="index-build-a")
    thread_a.start()
    assert a_started.wait(timeout=2)
    okf.mark_index_dirty(state_dir)
    thread_b = threading.Thread(target=ensure_index, name="index-build-b")
    thread_b.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert errors == []
    assert db_path.read_text(encoding="utf-8") == "fresh-with-new-okf"
    assert not okf.index_dirty_tokens(state_dir)
    assert lci_storage.index_build_lock_path(state_dir).exists()


def test_cli_build_cannot_overwrite_newer_native_rebuild(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    db_path = state_dir / "index.sqlite"
    db_path.parent.mkdir(parents=True)
    db_path.write_text("initial", encoding="utf-8")
    cli_started = threading.Event()
    native_started = threading.Event()
    statuses: list[int] = []
    errors: list[BaseException] = []

    def cli_build(*_args):  # type: ignore[no-untyped-def]
        cli_started.set()
        native_started.wait(timeout=0.2)
        db_path.write_text("stale-cli", encoding="utf-8")
        return [], []

    def native_build(*_args):  # type: ignore[no-untyped-def]
        native_started.set()
        db_path.write_text("fresh-native", encoding="utf-8")
        return [], []

    def run_cli() -> None:
        try:
            statuses.append(
                lci_cli.main(
                    [
                        "build",
                        "--root",
                        str(repo),
                        "--output-dir",
                        str(state_dir),
                        "--hermes-home",
                        str(hermes_home),
                    ],
                    build_index_fn=cli_build,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def run_native() -> None:
        try:
            lci_runtime._ensure_index(repo, build_index_fn=native_build)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    cli_thread = threading.Thread(target=run_cli)
    cli_thread.start()
    assert cli_started.wait(timeout=2)
    okf.mark_index_dirty(state_dir)
    native_thread = threading.Thread(target=run_native)
    native_thread.start()
    cli_thread.join(timeout=3)
    native_thread.join(timeout=3)

    assert not cli_thread.is_alive()
    assert not native_thread.is_alive()
    assert errors == []
    assert statuses == [0]
    assert db_path.read_text(encoding="utf-8") == "fresh-native"
    assert not okf.index_dirty_tokens(state_dir)


def test_generic_build_wrapper_does_not_deadlock_cli_or_runtime(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    monkeypatch.setattr(lci_storage, "INDEX_BUILD_LOCK_WAIT_SECONDS", 0.1)
    wrapper_calls: list[Path] = []

    def generic_wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        wrapper_calls.append(Path(args[1]))
        return lci_storage.build_index(*args, **kwargs)

    status = lci_cli.main(
        [
            "build",
            "--root",
            str(repo),
            "--output-dir",
            str(state_dir),
            "--hermes-home",
            str(hermes_home),
        ],
        build_index_fn=generic_wrapper,
    )
    capsys.readouterr()
    okf.mark_index_dirty(state_dir)
    _db_path, metadata = lci_runtime._ensure_index(repo, build_index_fn=generic_wrapper)

    assert status == 0
    assert metadata["rebuilt"] is True
    assert wrapper_calls == [state_dir, state_dir]
    assert not okf.index_dirty_tokens(state_dir)


def test_cli_search_refreshes_dirty_default_index(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    lci_storage.build_index(repo, state_dir, hermes_home)
    write(
        repo / "custom_skills" / "note-taking" / "late-okf-router" / "SKILL.md",
        """---
name: late-okf-router
description: Route newly completed late OKF lookups.
tags:
  - completion
---
# Late OKF router
""",
    )
    okf.mark_index_dirty(state_dir)

    status = lci_cli.main(
        [
            "search",
            "newly completed late OKF",
            "--json",
            "--from-hermes-config",
            "--hermes-home",
            str(hermes_home),
        ]
    )
    rows = json.loads(capsys.readouterr().out)

    assert status == 0
    assert any(row["id"] == "skill:late-okf-router" for row in rows)
    assert not okf.index_dirty_tokens(state_dir)


def test_cli_explicit_index_sqlite_is_never_auto_rebuilt(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, _state_dir = make_temp_repo(tmp_path)
    custom_state = tmp_path / "custom-db"
    db_path = custom_state / "index.sqlite"
    lci_storage.build_index(repo, custom_state, hermes_home)
    okf.mark_index_dirty(custom_state)
    build_calls: list[str] = []

    def unexpected_build(*_args):  # type: ignore[no-untyped-def]
        build_calls.append("called")
        raise AssertionError("explicit database must not be rebuilt")

    status = lci_cli.main(
        [
            "search",
            "Paperless review automation",
            "--json",
            "--db",
            str(db_path),
            "--hermes-home",
            str(hermes_home),
        ],
        build_index_fn=unexpected_build,
    )
    rows = json.loads(capsys.readouterr().out)

    assert status == 0
    assert rows
    assert build_calls == []
    assert len(okf.index_dirty_tokens(custom_state)) == 1


def test_index_build_lock_serializes_across_processes(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    lock_path = lci_storage.index_build_lock_path(state_dir)
    probe = """
import fcntl
import os
import sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('blocked')
else:
    print('acquired')
finally:
    os.close(fd)
"""
    with lci_storage.index_build_lock(state_dir):
        blocked = subprocess.run(
            [sys.executable, "-c", probe, str(lock_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    acquired = subprocess.run(
        [sys.executable, "-c", probe, str(lock_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert blocked.stdout.strip() == "blocked"
    assert acquired.stdout.strip() == "acquired"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_index_build_lock_resets_same_thread_state_after_fork(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state_dir = tmp_path / "state"
    monkeypatch.setattr(lci_storage, "INDEX_BUILD_LOCK_WAIT_SECONDS", 1.0)
    read_fd, write_fd = os.pipe()
    fork = getattr(os, "fork")

    with lci_storage.index_build_lock(state_dir):
        pid = fork()
        if pid == 0:  # pragma: no cover - assertions run in the parent
            os.close(read_fd)
            started = time.monotonic()
            try:
                with lci_storage.index_build_lock(state_dir):
                    elapsed = time.monotonic() - started
                child_payload = f"{elapsed:.6f}".encode()
            except BaseException as exc:
                child_payload = f"ERROR:{type(exc).__name__}:{exc}".encode()
            os.write(write_fd, child_payload)
            os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        time.sleep(0.2)

    parent_payload = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    _waited_pid, status = os.waitpid(pid, 0)

    assert os.WIFEXITED(status)
    assert not parent_payload.startswith("ERROR:"), parent_payload
    assert float(parent_payload) >= 0.15


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_index_build_lock_closes_other_thread_descriptor_after_fork(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state_dir = tmp_path / "state"
    monkeypatch.setattr(lci_storage, "INDEX_BUILD_LOCK_WAIT_SECONDS", 1.0)
    entered = threading.Event()
    release = threading.Event()

    def owner() -> None:
        with lci_storage.index_build_lock(state_dir):
            entered.set()
            release.wait(timeout=5)

    owner_thread = threading.Thread(target=owner)
    owner_thread.start()
    assert entered.wait(timeout=2)
    read_fd, write_fd = os.pipe()
    fork = getattr(os, "fork")
    pid = fork()
    if pid == 0:  # pragma: no cover - assertions run in the parent
        os.close(read_fd)
        started = time.monotonic()
        try:
            with lci_storage.index_build_lock(state_dir):
                elapsed = time.monotonic() - started
            payload = f"{elapsed:.6f}".encode()
        except BaseException as exc:
            payload = f"ERROR:{type(exc).__name__}:{exc}".encode()
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    time.sleep(0.2)
    release.set()
    owner_thread.join(timeout=2)
    parent_payload = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    _waited_pid, status = os.waitpid(pid, 0)

    assert not owner_thread.is_alive()
    assert os.WIFEXITED(status)
    assert not parent_payload.startswith("ERROR:"), parent_payload
    assert float(parent_payload) >= 0.15


def test_storage_import_does_not_require_platform_lock_modules() -> None:
    script = """
import builtins
original_import = builtins.__import__
def blocked_import(name, *args, **kwargs):
    if name in {'fcntl', 'msvcrt'}:
        raise ImportError(name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = blocked_import
import hermes_local_knowledge.storage as storage
assert storage._fcntl is None
assert storage._msvcrt is None
print('imported')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "imported"


def test_index_build_lock_uses_windows_fallback(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[int] = []

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_fd: int, mode: int, _length: int) -> None:
            calls.append(mode)

    monkeypatch.setattr(lci_storage, "_fcntl", None)
    monkeypatch.setattr(lci_storage, "_msvcrt", FakeMsvcrt)

    with lci_storage.index_build_lock(tmp_path / "state"):
        with lci_storage.index_build_lock(tmp_path / "state"):
            pass

    assert calls == [FakeMsvcrt.LK_NBLCK, FakeMsvcrt.LK_UNLCK]


def test_completed_okf_is_discoverable_on_next_normal_search(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    initial = json.loads(plugin._handle_search({"query": "paperless", "rebuild": True}))
    assert initial["success"] is True

    tool_name = "mcp__paperless__paperless_find_latest_document"
    schema = {"type": "object"}
    okf.upsert_tool_candidate(
        state_dir,
        tool_name=tool_name,
        toolset="paperless",
        schema=schema,
        args={},
    )
    claimed = okf.claim_candidates(state_dir, limit=1, claim_token="claim-1")
    assert [row["tool_name"] for row in claimed] == [tool_name]
    output = okf.okf_file_path(state_dir, tool_name)
    write(
        output,
        f"""---
artifact_type: tool_okf
tool: {tool_name}
toolset: paperless
schema_hash: {okf.schema_hash(schema)}
generated_at: '2026-07-10T12:00:00Z'
aliases:
  - find newest paperless document
triggers:
  - User asks for the latest matching Paperless document metadata.
---

# Tool OKF: paperless_find_latest_document

Route to this tool for metadata about the newest matching Paperless document.
""",
    )
    assert okf.mark_candidate_done(
        state_dir,
        tool_name=tool_name,
        claim_token="claim-1",
        okf_path=output,
    )

    result = json.loads(
        plugin._handle_search(
            {
                "query": "find newest paperless document",
                "artifact_type": "tool_okf",
            }
        )
    )

    assert result["success"] is True
    assert result["rebuilt"] is True
    assert [row["id"] for row in result["results"]] == [
        "tool_okf:mcp-paperless-paperless-find-latest-document"
    ]
    assert not okf.index_dirty_tokens(state_dir)


def test_handlers_return_json_errors_for_malformed_args() -> None:
    for handler in (
        plugin._handle_search,
        plugin._handle_get,
        plugin._handle_neighbors,
        plugin._handle_feedback,
        plugin._handle_usage_report,
    ):
        payload = json.loads(handler(None))
        assert payload["success"] is False
        assert payload["error"] == "args must be an object"


def test_search_get_and_neighbors_build_missing_index_in_state_dir(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    search = json.loads(
        plugin._handle_search(
            {"query": "paperless review automation", "limit": 5, "rebuild": True}
        )
    )
    assert search["success"] is True
    assert search["rebuilt"] is True
    assert search["root"] == str(repo.resolve())
    assert search["state_dir"] == str(state_dir.resolve())
    assert isinstance(search["usage_event_id"], int)
    ids = [row["id"] for row in search["results"]]
    assert "skill:paperless-review-automation" in ids
    assert (state_dir / "index.sqlite").exists()
    assert (state_dir / "usage.sqlite").exists()
    assert not (repo / "knowledge" / "index.sqlite").exists()

    script_search = json.loads(
        plugin._handle_search(
            {"query": "paperless review automation", "limit": 5, "artifact_type": "script"}
        )
    )
    assert script_search["success"] is True
    assert {row["type"] for row in script_search["results"]} == {"script"}
    assert [row["id"] for row in script_search["results"]] == ["script:scripts-paperless-review-helper-py"]

    fetched = json.loads(
        plugin._handle_get(
            {"artifact_id": "skill:paperless-review-automation", "include_neighbors": True}
        )
    )
    assert fetched["success"] is True
    assert fetched["artifact"]["title"] == "paperless-review-automation"
    assert isinstance(fetched["usage_event_id"], int)
    neighbor_ids = {row["id"] for row in fetched["neighbors"]}
    assert "skill:paperless-helper" in neighbor_ids

    neighbors = json.loads(
        plugin._handle_neighbors({"artifact_id": "skill:paperless-review-automation"})
    )
    assert neighbors["success"] is True
    assert isinstance(neighbors["usage_event_id"], int)
    assert any(row["edge_kind"] == "related_to" for row in neighbors["neighbors"])


def test_runtime_config_can_read_hermes_config_yaml(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
  custom_skill_dirs: '[custom_skills]'
  script_dirs: '[scripts]'
  known_entities: '[Paperless]'
  exclude_dir_names: '[build, dist]'
""",
    )

    cfg = plugin._runtime_config()

    assert cfg.source_root == repo.resolve()
    assert cfg.state_dir == state_dir.resolve()
    assert cfg.index_settings.custom_skill_dirs == ("custom_skills",)
    assert cfg.index_settings.script_dirs == ("scripts",)
    assert cfg.index_settings.known_entities == ("Paperless",)
    assert cfg.index_settings.exclude_dir_names == ("build", "dist")
    assert cfg.index_settings.include_markdown_docs is True


def test_runtime_config_can_use_configured_hermes_home(tmp_path, monkeypatch):
    base_home = tmp_path / "base_home"
    configured_home = tmp_path / "configured_home"
    repo, _hermes_home, state_dir = make_temp_repo(tmp_path)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(base_home))
    write(
        base_home / "config.yaml",
        f"""local_knowledge:
  hermes_home: {configured_home}
  source_root: {repo}
  state_dir: {state_dir}
""",
    )

    cfg = plugin._runtime_config()

    assert cfg.hermes_home == configured_home.resolve()
    assert cfg.source_root == repo.resolve()
    assert cfg.state_dir == state_dir.resolve()


def test_runtime_config_explicit_hermes_home_overrides_configured_hermes_home(tmp_path, monkeypatch):
    base_home = tmp_path / "base_home"
    configured_home = tmp_path / "configured_home"
    repo, _hermes_home, state_dir = make_temp_repo(tmp_path)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        base_home / "config.yaml",
        f"""local_knowledge:
  hermes_home: {configured_home}
  source_root: {repo}
  state_dir: {state_dir}
""",
    )

    cfg = plugin._runtime_config(hermes_home=base_home)

    assert cfg.hermes_home == base_home.resolve()
    assert cfg.source_root == repo.resolve()
    assert cfg.state_dir == state_dir.resolve()


def test_runtime_env_overrides_hermes_config_yaml(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    env_repo = tmp_path / "env_repo"
    env_state = tmp_path / "env_state"
    write(env_repo / "scripts" / "env_helper.py", '"""Environment selected helper."""\n')
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
""",
    )
    configure_env(monkeypatch, env_repo, hermes_home, env_state)

    payload = json.loads(plugin._handle_search({"query": "environment selected", "rebuild": True}))

    assert payload["success"] is True
    assert payload["root"] == str(env_repo.resolve())
    assert payload["state_dir"] == str(env_state.resolve())
    assert [row["id"] for row in payload["results"]] == ["script:scripts-env-helper-py"]


def test_handle_search_records_usage_context(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    db_path = tmp_path / "state" / "index.sqlite"
    captured: dict[str, object] = {}

    def fake_usage_context(kwargs):  # type: ignore[no-untyped-def]
        captured["usage_context_kwargs"] = kwargs
        return {"session_id": kwargs["session_id"]}

    def fake_record_usage(root_arg: Path, **kwargs):  # type: ignore[no-untyped-def]
        captured["record_root"] = root_arg
        captured["record_usage_kwargs"] = kwargs
        return 123

    def fake_search(_db_path: Path, query: str, limit: int = 8, artifact_type=None):  # type: ignore[no-untyped-def]
        captured["search_artifact_type"] = artifact_type
        return [{"id": "skill:demo", "type": "skill", "title": query}]

    deps = plugin.HandlerDeps(
        repo_root=lambda: root,
        ensure_index=lambda _root, *, rebuild=False: (db_path, {"rebuilt": rebuild, "index_exists": True}),
        search_index=fake_search,
        record_usage=fake_record_usage,
        usage_context=fake_usage_context,
    )

    payload = json.loads(
        lci_handlers._handle_search(
            {"query": "demo", "limit": 2, "rebuild": True, "artifact_type": "script"},
            deps=deps,
            session_id="session-123",
        )
    )

    assert payload["success"] is True
    assert payload["usage_event_id"] == 123
    assert captured["usage_context_kwargs"] == {"session_id": "session-123"}
    usage_kwargs = captured["record_usage_kwargs"]
    assert isinstance(usage_kwargs, dict)
    assert usage_kwargs["context"] == {"session_id": "session-123"}
    assert usage_kwargs["query"] == "demo"
    assert usage_kwargs["artifact_type"] == "script"
    assert usage_kwargs["db_path"] == db_path
    assert captured["search_artifact_type"] == "script"
    assert captured["record_root"] == root


def test_tuple_value_accepts_common_cli_list_strings():
    default = ("default",)

    assert plugin._tuple_value("skills", default) == ("skills",)
    assert plugin._tuple_value("skills, custom_skills", default) == (
        "skills",
        "custom_skills",
    )
    assert plugin._tuple_value("[skills]", default) == ("skills",)
    assert plugin._tuple_value("['skills', 'custom_skills']", default) == (
        "skills",
        "custom_skills",
    )
    assert plugin._tuple_value('["skills", "custom_skills"]', default) == (
        "skills",
        "custom_skills",
    )


def test_implicit_hermes_home_source_skips_root_markdown(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    write(hermes_home / "private_notes.md", "# Private Notes\n\nRoot Markdown should not be indexed implicitly.\n")
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    cfg = plugin._runtime_config()
    payload = json.loads(plugin._handle_search({"query": "private notes", "rebuild": True}))

    assert cfg.source_root == hermes_home.resolve()
    assert cfg.index_settings.include_markdown_docs is False
    assert payload["success"] is True
    assert payload["results"] == []
    assert (hermes_home / "local_knowledge" / "index.sqlite").exists()


def test_explicit_source_root_can_disable_markdown_docs(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    write(repo / "docs" / "private.md", "# Private Markdown\n\nShould be skipped when markdown docs are disabled.\n")
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
  include_markdown_docs: false
""",
    )

    payload = json.loads(plugin._handle_search({"query": "private markdown", "rebuild": True}))

    assert payload["success"] is True
    assert payload["results"] == []
    assert payload["include_markdown_docs_source"] == "config"


def test_implicit_hermes_home_source_warns_when_source_checkout_exists(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_home"
    (hermes_home / "hermes-agent").mkdir(parents=True)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    payload = json.loads(plugin._handle_search({"query": "anything", "rebuild": True}))

    assert payload["success"] is True
    assert any("local_knowledge.source_root is unset" in warning for warning in payload["warnings"])


def test_missing_artifact_returns_tool_error(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    payload = json.loads(plugin._handle_get({"artifact_id": "skill:nope", "rebuild": True}))

    assert payload["success"] is False
    assert "Artifact not found" in payload["error"]
    assert isinstance(payload["usage_event_id"], int)


def test_lookup_handlers_validate_required_fields():
    search = json.loads(plugin._handle_search({"query": ""}))
    fetched = json.loads(plugin._handle_get({"artifact_id": ""}))
    neighbors = json.loads(plugin._handle_neighbors({"artifact_id": ""}))

    assert search["success"] is False
    assert search["error"] == "query is required"
    assert fetched["success"] is False
    assert fetched["error"] == "artifact_id is required"
    assert neighbors["success"] is False
    assert neighbors["error"] == "artifact_id is required"


def test_neighbors_missing_artifact_returns_tool_error(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    payload = json.loads(plugin._handle_neighbors({"artifact_id": "skill:nope", "rebuild": True}))

    assert payload["success"] is False
    assert "Artifact not found" in payload["error"]
    assert isinstance(payload["usage_event_id"], int)


def test_empty_usage_report_before_any_lookup_returns_zero_counts(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    payload = json.loads(plugin._handle_usage_report({"days": 7, "limit": 5}))

    assert payload["success"] is True
    assert payload["total_events"] == 0
    assert payload["feedback_count"] == 0
    assert payload["improvement_candidates"] == []
    assert (state_dir / "usage.sqlite").exists()


def test_feedback_rejects_invalid_rating_and_event_id():
    invalid_rating = json.loads(plugin._handle_feedback({"rating": "great"}))
    invalid_event_id = json.loads(plugin._handle_feedback({"rating": "useful", "event_id": "abc"}))

    assert invalid_rating["success"] is False
    assert "rating must be one of" in invalid_rating["error"]
    assert invalid_event_id["success"] is False
    assert invalid_event_id["error"] == "event_id must be an integer when provided"


def test_lookup_handlers_return_json_errors_for_corrupt_existing_index(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    state_dir.mkdir(parents=True)
    (state_dir / "index.sqlite").write_text("not a sqlite db", encoding="utf-8")

    search = json.loads(plugin._handle_search({"query": "paperless"}))
    fetched = json.loads(plugin._handle_get({"artifact_id": "skill:paperless-review-automation"}))
    neighbors = json.loads(plugin._handle_neighbors({"artifact_id": "skill:paperless-review-automation"}))

    assert search["success"] is False
    assert "knowledge_search failed" in search["error"]
    assert fetched["success"] is False
    assert "knowledge_get failed" in fetched["error"]
    assert neighbors["success"] is False
    assert "knowledge_neighbors failed" in neighbors["error"]


def test_feedback_and_usage_report_close_loop(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    search = json.loads(
        plugin._handle_search({"query": "paperless review automation", "limit": 3, "rebuild": True})
    )
    zero = json.loads(plugin._handle_search({"query": "zzzzzzzz unlikely", "limit": 3}))
    assert zero["success"] is True
    assert zero["results"] == []

    feedback = json.loads(
        plugin._handle_feedback(
            {
                "event_id": search["usage_event_id"],
                "rating": "wrong_artifact",
                "artifact_id": "skill:paperless-review-automation",
                "query": "paperless review automation",
                "note": "test feedback",
            }
        )
    )
    assert feedback["success"] is True
    assert isinstance(feedback["feedback_id"], int)

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    assert report["success"] is True
    assert report["total_events"] >= 3
    assert report["feedback_count"] == 1
    assert report["live_total_events"] == report["total_events"]
    assert report["root_breakdown"][0]["root_scope"] == "live"
    assert any(row["query"] == "zzzzzzzz unlikely" for row in report["zero_result_queries"])
    assert any(row["query"] == "zzzzzzzz unlikely" for row in report["unresolved_zero_result_queries"])
    assert any(row["rating"] == "wrong_artifact" for row in report["recent_negative_feedback"])
    assert any(row["rating"] == "wrong_artifact" for row in report["live_recent_negative_feedback"])
    assert any(item["type"] == "zero_result_query" for item in report["improvement_candidates"])
    assert any(item["type"] == "feedback_wrong_artifact" for item in report["improvement_candidates"])
    assert report["latest_index_metadata"]["plugin_version"] == hermes_local_knowledge.__version__
    assert report["latest_index_metadata"]["source_root_source"] == "env"
    assert report["latest_index_metadata"]["index_artifact_count"] >= 3
    assert report["latest_index_metadata"]["index_artifact_counts"]["skill"] == 2
    assert report["recent_builds"]
    assert report["recent_builds"][0]["index_artifact_counts"]["script"] == 1


def test_usage_report_separates_roots_and_suppresses_resolved_zero_results(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    now = datetime.now(timezone.utc)

    def ts(delta: timedelta) -> str:
        return (now + delta).isoformat(timespec="seconds").replace("+00:00", "Z")

    stamps = iter(
        [
            ts(timedelta(days=-6)),
            ts(timedelta(days=-5)),
            ts(timedelta(days=-2)),
            ts(timedelta(days=-4)),
            ts(timedelta(days=-1, hours=-1)),
            ts(timedelta(hours=-3)),
            ts(timedelta(hours=-2)),
            ts(timedelta(hours=-1)),
            ts(timedelta()),
        ]
    )
    monkeypatch.setattr(lci_telemetry, "_utc_now", lambda: next(stamps))
    usage_db_path = state_dir / "usage.sqlite"

    plugin._record_usage(repo, tool="knowledge_search", success=True, query="fixed query", result_count=0)
    plugin._record_usage(repo, tool="knowledge_search", success=True, query="fixed query", result_count=2)
    plugin._record_usage(repo, tool="knowledge_search", success=True, query="still missing", result_count=0)
    plugin._record_usage(repo, tool="knowledge_search", success=False, query="old live", error="old live error")
    plugin._record_usage(repo, tool="knowledge_search", success=False, query="recent live", error="recent live error")
    plugin._record_usage(repo, tool="knowledge_search", success=True, query="XXXX", result_count=0)
    plugin._record_usage(
        Path("/tmp/pytest-of-alex/router-test/repo"),
        tool="knowledge_search",
        success=False,
        query="test failure",
        error="test root error",
        usage_db_path=usage_db_path,
    )
    plugin._record_usage(
        Path("/tmp/pytest-of-alex/router-test/repo"),
        tool="knowledge_search",
        success=True,
        query="test zero",
        result_count=0,
        usage_db_path=usage_db_path,
    )

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    scopes = {row["root_scope"]: row for row in report["root_breakdown"]}
    assert scopes["live"]["count"] == 6
    assert scopes["test_tmp"]["count"] == 2
    assert report["live_total_events"] == 6
    assert report["total_events"] == 8
    assert [row["query"] for row in report["resolved_zero_result_queries"]] == ["fixed query"]
    assert {row["query"] for row in report["unresolved_zero_result_queries"]} == {"still missing", "XXXX"}
    assert [row["query"] for row in report["active_zero_result_queries"]] == ["still missing"]
    assert [row["query"] for row in report["probe_zero_result_queries"]] == ["XXXX"]
    assert all(row["query"] != "test zero" for row in report["unresolved_zero_result_queries"])
    assert {row["error"] for row in report["live_errors"]} == {"old live error", "recent live error"}
    assert [row["error"] for row in report["recent_live_errors"]] == ["recent live error"]
    candidate_queries = {row.get("query") for row in report["improvement_candidates"]}
    candidate_errors = {row.get("error") for row in report["improvement_candidates"] if row.get("error")}
    assert "still missing" in candidate_queries
    assert "fixed query" not in candidate_queries
    assert "XXXX" not in candidate_queries
    assert "test zero" not in candidate_queries
    assert candidate_errors == {"recent live error"}


def test_usage_report_buckets_unknown_feedback_ratings(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    plugin._record_feedback(repo, rating="great", event_id=None, query="", artifact_id="", note="legacy", context={})
    plugin._record_feedback(repo, rating="other", event_id=None, query="", artifact_id="", note="current", context={})

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    raw_ratings = {row["rating"]: row["count"] for row in report["feedback_by_rating"]}
    bucketed_ratings = {row["rating"]: row["count"] for row in report["feedback_rating_buckets"]}
    assert raw_ratings["great"] == 1
    assert bucketed_ratings["other"] == 2
    assert len(report["unknown_feedback_ratings"]) == 1
    assert report["unknown_feedback_ratings"][0]["rating"] == "great"
    assert report["unknown_feedback_ratings"][0]["count"] == 1


def test_usage_report_suppresses_negative_feedback_after_later_useful_feedback(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    now = datetime.now(timezone.utc)

    def ts(delta: timedelta) -> str:
        return (now + delta).isoformat(timespec="seconds").replace("+00:00", "Z")

    stamps = iter(
        [
            ts(timedelta(days=-4)),
            ts(timedelta(days=-4, seconds=1)),
            ts(timedelta(days=-1)),
            ts(timedelta(days=-1, seconds=1)),
            ts(timedelta()),
        ]
    )
    monkeypatch.setattr(lci_telemetry, "_utc_now", lambda: next(stamps))

    old_event = plugin._record_usage(
        repo,
        tool="knowledge_search",
        success=True,
        query="stale feedback query",
        result_count=2,
    )
    plugin._record_feedback(
        repo,
        rating="noisy",
        event_id=old_event,
        query="",
        artifact_id="",
        note="old ranking was noisy",
        context={},
    )
    useful_event = plugin._record_usage(
        repo,
        tool="knowledge_search",
        success=True,
        query="stale feedback query",
        result_count=2,
    )
    plugin._record_feedback(
        repo,
        rating="useful",
        event_id=useful_event,
        query="",
        artifact_id="",
        note="later check was useful",
        context={},
    )

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    assert report["live_recent_negative_feedback"][0]["effective_query"] == "stale feedback query"
    assert report["resolved_negative_feedback"][0]["effective_query"] == "stale feedback query"
    assert report["unresolved_negative_feedback"] == []
    assert all(item["type"] != "feedback_noisy" for item in report["improvement_candidates"])


def test_usage_report_recent_builds_exclude_failed_build_attempts(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    search = json.loads(plugin._handle_search({"query": "paperless review automation", "rebuild": True}))
    assert search["success"] is True
    plugin._record_usage(
        repo,
        tool="cli_build",
        client="cli",
        success=False,
        rebuilt=False,
        error="simulated failed build",
        db_path=state_dir / "index.sqlite",
        usage_db_path=state_dir / "usage.sqlite",
        index_metadata={
            "plugin_version": hermes_local_knowledge.__version__,
            "source_root_source": "config",
            "artifact_count": 999,
            "artifact_counts_by_type": {"skill": 999},
            "edge_count": 999,
            "build_duration_ms": 12,
        },
    )

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    assert report["recent_builds"]
    assert all(row["rebuilt"] == 1 for row in report["recent_builds"])
    assert all(row["index_artifact_count"] != 999 for row in report["recent_builds"])


def test_usage_report_persists_index_metadata_errors(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    event_id = plugin._record_usage(
        repo,
        tool="knowledge_search",
        success=True,
        query="corrupt index probe",
        result_count=0,
        db_path=state_dir / "index.sqlite",
        index_metadata={
            "plugin_version": hermes_local_knowledge.__version__,
            "source_root_source": "env",
            "state_dir_source": "env",
            "index_exists": True,
            "index_mtime": "2026-01-01T00:00:00Z",
            "index_metadata_error": "sqlite stats failed: DatabaseError: malformed database",
        },
    )
    assert isinstance(event_id, int)

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))

    assert report["latest_index_metadata"]["index_exists"] == 1
    assert "malformed database" in report["latest_index_metadata"]["index_metadata_error"]


def test_usage_db_migrates_preserved_legacy_schema(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)
    state_dir.mkdir(parents=True)
    conn = sqlite3.connect(state_dir / "usage.sqlite")
    try:
        conn.execute(
            "CREATE TABLE usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, tool TEXT NOT NULL, query TEXT)"
        )
        conn.execute("CREATE TABLE feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, rating TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()

    search = json.loads(plugin._handle_search({"query": "paperless review automation", "limit": 3, "rebuild": True}))
    assert search["success"] is True
    assert isinstance(search["usage_event_id"], int)

    feedback = json.loads(plugin._handle_feedback({"event_id": search["usage_event_id"], "rating": "useful"}))
    assert feedback["success"] is True
    assert isinstance(feedback["feedback_id"], int)

    report = json.loads(plugin._handle_usage_report({"days": 30, "limit": 10}))
    assert report["success"] is True
    assert report["total_events"] >= 2

    conn = sqlite3.connect(state_dir / "usage.sqlite")
    try:
        usage_columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
        feedback_columns = {row[1] for row in conn.execute("PRAGMA table_info(feedback)")}
    finally:
        conn.close()
    assert {"success", "latency_ms", "db_path", "top_ids_json"} <= usage_columns
    assert {
        "client",
        "plugin_version",
        "source_root_source",
        "index_artifact_count",
        "index_artifact_counts_json",
        "index_exists",
        "index_metadata_error",
        "build_duration_ms",
    } <= usage_columns
    assert {"event_id", "artifact_id", "session_id", "root"} <= feedback_columns
