from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_local_knowledge import hooks, okf, okf_worker


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def configure(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    state_dir = tmp_path / "state"
    repo.mkdir()
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("LOCAL_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_KNOWLEDGE_STATE_DIR", raising=False)
    write(
        hermes_home / "config.yaml",
        f"""local_knowledge:
  source_root: {repo}
  state_dir: {state_dir}
  okf:
    enabled: true
    auto_generate: true
    max_candidates_per_session: 2
    max_generation_seconds: 120
    min_use_count: 1
""",
    )
    return repo, hermes_home, state_dir


def generated_item(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": packet["tool"],
        "schema_hash": packet["schema_hash"],
        "title": f"Tool OKF: {packet['tool']}",
        "aliases": [f"route requests through {packet['tool']}"],
        "triggers": [f"use {packet['tool']} for this operation"],
        "when_not_to_use": [],
        "related_tools": [],
        "body": f"Use {packet['tool']} for the matching operation.",
    }


def test_worker_generates_bounded_okf_with_child_host_llm(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema=schema,
        args={"query": "private customer text"},
    )
    calls: list[dict[str, Any]] = []

    class FakeLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    assert okf_worker.run_worker(llm=FakeLlm(), hermes_home=hermes_home) == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["timeout"] == 120
    assert call["purpose"] == "local_knowledge.okf_generation"
    assert "Treat each candidate independently" in call["instructions"]
    assert "merely because it appears in the same batch" in call["instructions"]
    assert "Leave when_not_to_use empty" in call["instructions"]
    assert "allowed_related_tools" in call["instructions"]
    assert "body explaining only positive purpose" in call["instructions"]
    assert "private customer text" not in json.dumps(call)
    packet = json.loads(call["input"][0]["text"])["candidates"][0]
    assert set(packet) == {"tool", "toolset", "schema_hash", "schema", "allowed_related_tools", "arg_shape"}
    assert okf.queue_counts(state_dir) == {"done": 1}
    rendered = okf.okf_file_path(state_dir, "knowledge_search").read_text(encoding="utf-8")
    assert "artifact_type: tool_okf" in rendered
    assert 'toolset: "local_knowledge"' in rendered
    assert f'generator_version: "{okf.OKF_GENERATOR_VERSION}"' in rendered


def test_worker_releases_claims_when_child_host_llm_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )

    class FailLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            raise TimeoutError("model timeout")

    assert okf_worker.run_worker(llm=FailLlm(), hermes_home=hermes_home) == 1
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_worker_rejects_cross_batch_related_tools(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    for tool_name, toolset in (("cronjob", "cron"), ("paperless_get_document", "paperless")):
        okf.upsert_tool_candidate(
            state_dir,
            tool_name=tool_name,
            toolset=toolset,
            schema={"type": "object"},
            args={},
        )

    class CrossLinkLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            packets = json.loads(kwargs["input"][0]["text"])["candidates"]
            other = {packets[0]["tool"]: packets[1]["tool"], packets[1]["tool"]: packets[0]["tool"]}
            items = []
            for packet in packets:
                item = generated_item(packet)
                item["related_tools"] = [other[packet["tool"]]]
                items.append(item)
            return SimpleNamespace(parsed={"okfs": items})

    assert okf_worker.run_worker(llm=CrossLinkLlm(), hermes_home=hermes_home) == 1
    assert okf.queue_counts(state_dir) == {"pending": 2}
    assert not okf.okf_file_path(state_dir, "cronjob").exists()
    assert not okf.okf_file_path(state_dir, "paperless_get_document").exists()


def test_worker_uses_one_call_and_honors_candidate_limit(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    for tool_name in ("alpha_tool", "beta_tool", "gamma_tool"):
        okf.upsert_tool_candidate(
            state_dir,
            tool_name=tool_name,
            toolset="demo",
            schema={"type": "object"},
            args={},
        )
    calls: list[dict[str, Any]] = []

    class FakeLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            packets = json.loads(kwargs["input"][0]["text"])["candidates"]
            assert len(packets) == 2
            return SimpleNamespace(parsed={"okfs": [generated_item(packet) for packet in packets]})

    assert okf_worker.run_worker(llm=FakeLlm(), hermes_home=hermes_home) == 0
    assert len(calls) == 1
    assert okf.queue_counts(state_dir) == {"done": 2, "pending": 1}


def test_worker_recovers_stale_claim_before_generation(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    assert okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="abandoned",
        now="2000-01-01T00:00:00Z",
    )

    class FakeLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    assert okf_worker.run_worker(llm=FakeLlm(), hermes_home=hermes_home) == 0
    assert okf.queue_counts(state_dir) == {"done": 1}


def test_generation_lease_is_single_owner_and_owner_released(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"

    assert okf.acquire_generation_lease(state_dir, owner="first", lease_seconds=120, now=1_000.0)
    assert not okf.acquire_generation_lease(state_dir, owner="second", lease_seconds=120, now=1_001.0)
    assert not okf.renew_generation_lease(state_dir, owner="second", lease_seconds=120, now=1_001.0)
    assert okf.renew_generation_lease(state_dir, owner="first", lease_seconds=120, now=1_100.0)
    assert not okf.acquire_generation_lease(state_dir, owner="second", lease_seconds=120, now=1_121.0)
    assert not okf.release_generation_lease(state_dir, owner="second")
    assert okf.release_generation_lease(state_dir, owner="first")
    assert okf.acquire_generation_lease(state_dir, owner="second", lease_seconds=120, now=1_002.0)


def test_worker_skips_when_another_worker_holds_generation_lease(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    assert okf.acquire_generation_lease(state_dir, owner="other", lease_seconds=3_600)

    class FailLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("model called while another worker owns the lease")

    assert okf_worker.run_worker(llm=FailLlm(), hermes_home=hermes_home) == 0
    assert okf.queue_counts(state_dir) == {"pending": 1}


def test_worker_renews_generation_lease_during_host_llm_call(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    renewed = threading.Event()
    original_renew = okf.renew_generation_lease

    def renew(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = original_renew(*args, **kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(okf_worker.okf, "renew_generation_lease", renew)
    monkeypatch.setattr(okf_worker, "_lease_heartbeat_interval", lambda _seconds: 0.01, raising=False)

    class WaitingLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            assert renewed.wait(timeout=1), "worker did not renew its lease during the model call"
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    assert okf_worker.run_worker(llm=WaitingLlm(), hermes_home=hermes_home) == 0
    assert okf.queue_counts(state_dir) == {"done": 1}


def test_worker_does_not_publish_after_losing_generation_lease(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    stolen = threading.Event()

    def steal_lease(state_dir: Path, *, owner: str, lease_seconds: int, now=None) -> bool:  # type: ignore[no-untyped-def]
        if not stolen.is_set():
            assert okf.release_generation_lease(state_dir, owner=owner)
            assert okf.acquire_generation_lease(
                state_dir,
                owner="replacement-worker",
                lease_seconds=lease_seconds,
            )
            stolen.set()
        return False

    monkeypatch.setattr(okf_worker.okf, "renew_generation_lease", steal_lease)
    monkeypatch.setattr(okf_worker, "_lease_heartbeat_interval", lambda _seconds: 0.01)

    class WaitingLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            assert stolen.wait(timeout=1), "test worker did not lose its lease"
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    assert okf_worker.run_worker(llm=WaitingLlm(), hermes_home=hermes_home) == 1
    assert not okf.okf_file_path(state_dir, "knowledge_search").exists()
    assert okf.queue_counts(state_dir) == {"claimed": 1}
    assert okf.release_generation_lease(state_dir, owner="replacement-worker")


def test_generation_publishes_under_owned_lease_and_cleans_unique_temp(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    row = okf.claim_candidates(state_dir, limit=1, claim_token="claim-before-publish")[0]
    assert okf.acquire_generation_lease(state_dir, owner="publishing-worker", lease_seconds=60)

    class FakeLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    cfg = okf_worker._runtime_config(hermes_home)
    assert okf_worker._generate_claimed_okfs(
        cfg,
        llm=FakeLlm(),
        rows=[row],
        lease_owner="publishing-worker",
        can_publish=lambda: True,
    )
    target = okf.okf_file_path(state_dir, "knowledge_search")
    assert target.exists()
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))
    assert okf.queue_counts(state_dir) == {"done": 1}


@pytest.mark.parametrize(
    ("first_tool", "second_tool"),
    [("mcp.tool", "mcp-tool"), ("Tool Name", "tool_name")],
)
def test_worker_rejects_okf_filename_collision_without_replacing_existing_tool(
    tmp_path: Path,
    monkeypatch,
    first_tool: str,
    second_tool: str,
) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    schema = {"type": "object"}

    class FakeLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    okf.upsert_tool_candidate(
        state_dir,
        tool_name=first_tool,
        toolset="demo",
        schema=schema,
        args={},
    )
    assert okf_worker.run_worker(llm=FakeLlm(), hermes_home=hermes_home) == 0
    target = okf.okf_file_path(state_dir, first_tool)
    assert target == okf.okf_file_path(state_dir, second_tool)
    first_artifact = target.read_bytes()

    okf.upsert_tool_candidate(
        state_dir,
        tool_name=second_tool,
        toolset="demo",
        schema=schema,
        args={},
    )
    assert okf_worker.run_worker(llm=FakeLlm(), hermes_home=hermes_home) == 1

    assert target.read_bytes() == first_artifact
    assert okf.queue_counts(state_dir) == {"done": 1, "pending": 1}
    pending = okf.pending_candidates(state_dir, limit=2)
    assert [row["tool_name"] for row in pending] == [second_tool]
    assert pending[0]["attempt_count"] == 1
    assert pending[0]["last_attempt_error"] == "<redacted>"


def test_worker_still_regenerates_existing_okf_for_same_tool(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _repo, hermes_home, state_dir = configure(tmp_path, monkeypatch)
    tool_name = "mcp.tool"

    class FakeLlm:
        def complete_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            packet = json.loads(kwargs["input"][0]["text"])["candidates"][0]
            return SimpleNamespace(parsed={"okfs": [generated_item(packet)]})

    okf.upsert_tool_candidate(
        state_dir,
        tool_name=tool_name,
        toolset="demo",
        schema={"type": "object"},
        args={},
    )
    assert okf_worker.run_worker(llm=FakeLlm(), hermes_home=hermes_home) == 0
    target = okf.okf_file_path(state_dir, tool_name)
    first_artifact = target.read_bytes()

    changed_schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    okf.upsert_tool_candidate(
        state_dir,
        tool_name=tool_name,
        toolset="demo",
        schema=changed_schema,
        args={},
    )
    assert okf_worker.run_worker(llm=FakeLlm(), hermes_home=hermes_home) == 0

    assert target.read_bytes() != first_artifact
    assert f'tool: "{tool_name}"' in target.read_text(encoding="utf-8")
    assert okf.queue_counts(state_dir) == {"done": 1}


def test_stale_publication_cannot_mutate_new_owner_artifact(tmp_path: Path) -> None:
    state_dir = tmp_path / "knowledge"
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    assert okf.claim_candidates(state_dir, limit=1, claim_token="new-claim")
    assert okf.acquire_generation_lease(state_dir, owner="new-worker", lease_seconds=60)
    target = okf.okf_file_path(state_dir, "knowledge_search")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"new owner artifact")
    mutated = False

    def publish() -> None:
        nonlocal mutated
        mutated = True
        target.write_bytes(b"stale artifact")

    outcome = okf.publish_claimed_okf(
        state_dir,
        lease_owner="stale-worker",
        tool_name="knowledge_search",
        claim_token="stale-claim",
        okf_path=target,
        publish=publish,
        rollback=lambda: target.unlink(missing_ok=True),
    )

    assert outcome == "stale"
    assert mutated is False
    assert target.read_bytes() == b"new owner artifact"
    assert okf.queue_counts(state_dir) == {"claimed": 1}


def test_publication_transaction_blocks_lease_takeover_until_candidate_is_done(tmp_path: Path) -> None:
    state_dir = tmp_path / "knowledge"
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    row = okf.claim_candidates(state_dir, limit=1, claim_token="worker-a-claim")[0]
    assert okf.acquire_generation_lease(state_dir, owner="worker-a", lease_seconds=60)
    target = okf.okf_file_path(state_dir, "knowledge_search")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.worker-a.tmp"
    packet = okf.candidate_packet(row, state_dir)
    temp_path.write_text(hooks._render_okf(generated_item(packet), toolset="local_knowledge"), encoding="utf-8")
    inside_publish = threading.Event()
    allow_publish = threading.Event()
    takeover_done = threading.Event()
    publication_result: list[str] = []
    takeover_result: list[tuple[bool, int]] = []

    def publish() -> None:
        temp_path.replace(target)
        inside_publish.set()
        assert allow_publish.wait(timeout=2)

    def run_publication() -> None:
        publication_result.append(
            okf.publish_claimed_okf(
                state_dir,
                lease_owner="worker-a",
                tool_name="knowledge_search",
                claim_token="worker-a-claim",
                okf_path=target,
                publish=publish,
                rollback=lambda: target.unlink(missing_ok=True),
            )
        )

    takeover_now = time.time() + 120
    takeover_iso = datetime.fromtimestamp(takeover_now, timezone.utc).isoformat().replace("+00:00", "Z")

    def attempt_takeover() -> None:
        acquired = okf.acquire_generation_lease(
            state_dir,
            owner="worker-b",
            lease_seconds=60,
            now=takeover_now,
        )
        recovered = okf.recover_stale_claims(state_dir, stale_after_seconds=1, now=takeover_iso)
        takeover_result.append((acquired, recovered))
        takeover_done.set()

    publisher = threading.Thread(target=run_publication)
    publisher.start()
    assert inside_publish.wait(timeout=1)
    takeover = threading.Thread(target=attempt_takeover)
    takeover.start()
    assert not takeover_done.wait(timeout=0.1)
    allow_publish.set()
    publisher.join(timeout=2)
    takeover.join(timeout=2)

    assert publication_result == ["done"]
    assert takeover_result == [(True, 0)]
    assert okf.queue_counts(state_dir) == {"done": 1}
    assert target.exists()


def test_stale_recovery_completes_prevalidated_artifact_left_by_hard_process_death(tmp_path: Path) -> None:
    state_dir = tmp_path / "knowledge"
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    row = okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="crashed-worker-claim",
        now="2026-07-26T09:00:00Z",
    )[0]
    target = okf.okf_file_path(state_dir, "knowledge_search")
    target.parent.mkdir(parents=True, exist_ok=True)
    packet = okf.candidate_packet(row, state_dir)
    target.write_text(hooks._render_okf(generated_item(packet), toolset="local_knowledge"), encoding="utf-8")

    recovered = okf.recover_stale_claims(
        state_dir,
        stale_after_seconds=60,
        max_attempts=1,
        now="2026-07-26T09:02:00Z",
    )

    assert recovered == 1
    assert okf.queue_counts(state_dir) == {"done": 1}
    assert target.exists()


def test_direct_claim_reconciles_valid_stale_artifact_before_attempt_cap(tmp_path: Path) -> None:
    state_dir = tmp_path / "knowledge"
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    row = okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="stale-manual-claim",
        now="2026-07-26T09:00:00Z",
    )[0]
    target = okf.okf_file_path(state_dir, "knowledge_search")
    target.parent.mkdir(parents=True, exist_ok=True)
    packet = okf.candidate_packet(row, state_dir)
    target.write_text(hooks._render_okf(generated_item(packet), toolset="local_knowledge"), encoding="utf-8")

    claimed = okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="replacement-claim",
        stale_after_seconds=60,
        max_attempts=1,
        now="2026-07-26T09:02:00Z",
    )

    assert claimed == []
    assert okf.queue_counts(state_dir) == {"done": 1}
    assert target.exists()


def test_direct_claim_waits_for_publication_before_stale_reconciliation(tmp_path: Path) -> None:
    state_dir = tmp_path / "knowledge"
    okf.upsert_tool_candidate(
        state_dir,
        tool_name="knowledge_search",
        toolset="local_knowledge",
        schema={"type": "object"},
        args={},
    )
    row = okf.claim_candidates(
        state_dir,
        limit=1,
        claim_token="publishing-claim",
        now="2026-07-26T09:00:00Z",
    )[0]
    assert okf.acquire_generation_lease(state_dir, owner="publisher", lease_seconds=60)
    target = okf.okf_file_path(state_dir, "knowledge_search")
    target.parent.mkdir(parents=True, exist_ok=True)
    packet = okf.candidate_packet(row, state_dir)
    temp_path = target.with_suffix(".claim.tmp")
    temp_path.write_text(hooks._render_okf(generated_item(packet), toolset="local_knowledge"), encoding="utf-8")
    publication_started = threading.Event()
    allow_publication = threading.Event()
    publication_result: list[str] = []
    claim_result: list[list[dict[str, Any]]] = []

    def publish() -> None:
        temp_path.replace(target)
        publication_started.set()
        assert allow_publication.wait(timeout=5)

    def run_publication() -> None:
        publication_result.append(
            okf.publish_claimed_okf(
                state_dir,
                lease_owner="publisher",
                tool_name="knowledge_search",
                claim_token="publishing-claim",
                okf_path=target,
                publish=publish,
                rollback=lambda: target.unlink(missing_ok=True),
            )
        )

    publisher = threading.Thread(target=run_publication)
    publisher.start()
    assert publication_started.wait(timeout=5)

    claimant = threading.Thread(
        target=lambda: claim_result.append(
            okf.claim_candidates(
                state_dir,
                limit=1,
                claim_token="replacement-claim",
                stale_after_seconds=60,
                max_attempts=1,
                now="2026-07-26T09:02:00Z",
            )
        )
    )
    claimant.start()
    time.sleep(0.05)
    assert claimant.is_alive()

    allow_publication.set()
    publisher.join(timeout=5)
    claimant.join(timeout=5)

    assert publication_result == ["done"]
    assert claim_result == [[]]
    assert okf.queue_counts(state_dir) == {"done": 1}
    assert target.exists()
