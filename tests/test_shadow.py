from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_local_knowledge import index, shadow, shadow_sources
from hermes_local_knowledge.config import Config, IndexSettings, VerifiedRoutingSettings


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    root, home, state = (tmp_path / name for name in ("corpus", "profile", "state"))
    root.mkdir()
    home.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "quartz-restart.md").write_text(
        "# Quartz service restart runbook\nRestart Quartz safely after configuration changes.\n"
        "Validate the Quartz service health after restarting.\n", encoding="utf-8")
    (root / "docs" / "quartz-backup.md").write_text(
        "# Quartz service backup runbook\nBack up Quartz data; this is not a restart procedure.\n",
        encoding="utf-8")
    config = Config(source_root=root, hermes_home=home, state_dir=state,
                    index_settings=IndexSettings(), verified_routing=VerifiedRoutingSettings(mode="shadow"))
    index.build_index(root, state, home, config.index_settings)
    return config


def capture(cfg: Config, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"user_request": "Find the Quartz service restart runbook.", "query": "Quartz restart runbook",
              "artifact_type": "", "session_id": "session-1", "task_id": "task-1", "turn_id": "turn-1",
              "baseline_ids": ["runbook:docs-quartz-backup"]}
    values.update(overrides)
    return shadow.observe(cfg, **values)


def rows(cfg: Config) -> list[dict[str, Any]]:
    with shadow._connect(cfg) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM cases ORDER BY id")]


def mutate(cfg: Config, sql: str, params: tuple[Any, ...] = ()) -> None:
    with shadow._connect(cfg, create=True) as conn:
        conn.execute(sql, params)


def citation(source: dict[str, Any]) -> dict[str, Any]:
    return {**{key: source[key] for key in ("id", "locator", "sha256")},
            "start_line": 1, "end_line": 2}


class Model:
    """Exercises the real action loop, not private worker helpers."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        packet = json.loads(kwargs["input"][0]["text"])
        assert kwargs["json_mode"] is True
        assert 0 < kwargs["timeout"] <= 300
        assert kwargs["max_tokens"] == (1800 if kwargs["purpose"].endswith("investigator") else 4000)
        assert "json_schema" not in kwargs
        if kwargs["purpose"].endswith("verifier"):
            answer = {"verdict": "verified", "route_ids": packet["proposal"]["route_ids"],
                      "near_miss_id": packet["near_miss_id"], "task_supported": True,
                      "near_miss_rejected": True, "lookup_supported": True,
                      "baseline_review": [{"id": i, "disposition": "not_useful", "covered_by": [],
                                           "reason": "Backup procedure does not address restarting Quartz."}
                                          for i in packet["baseline_ids"]],
                      "citations": [citation(s) for s in packet["sources"]]}
        elif not packet["sources"]:
            answer = {"action": "read", "ids": [next(row["id"] for row in packet["candidates"]
                                                       if "restart" in row["id"])]}
        else:
            source = next(s for s in packet["sources"] if "restart" in s["id"])
            answer = {"action": "propose", "route_ids": [source["id"]], "citations": [citation(source)]}
        return SimpleNamespace(parsed=answer, usage={"input_tokens": 10, "output_tokens": 5,
                                                     "raw_response": "MUST NOT PERSIST"})


def complete(cfg: Config, model: Any = None) -> dict[str, Any]:
    capture(cfg)
    shadow.finish_session(cfg, "session-1")
    return shadow.run_batch(cfg, llm=model or Model())


def test_off_and_read_only_checks_create_no_shadow_state(cfg: Config) -> None:
    off = replace(cfg, verified_routing=VerifiedRoutingSettings())
    assert capture(off) == {"status": "off"}
    shadow.finish_session(off, "session-1")
    assert not shadow.has_work(off)
    assert shadow.run_batch(off, llm=Model()) == {"claimed": 0, "verified": 0, "unresolved": 0}
    assert shadow.report(off)["mode"] == "off"
    assert not shadow.has_work(cfg)
    assert shadow.report(cfg)["cases"] == {}
    assert not (cfg.state_dir / "shadow").exists()


def test_end_to_end_read_sources_verify_reuse_no_public_writes(cfg: Config) -> None:
    before = hashlib.sha256((cfg.state_dir / "index.sqlite").read_bytes()).hexdigest()
    model = Model()
    result = complete(cfg, model)
    assert result == {"claimed": 1, "verified": 1, "unresolved": 0}
    assert len(model.calls) == 3
    investigator = json.loads(model.calls[1]["input"][0]["text"])
    verifier = json.loads(model.calls[2]["input"][0]["text"])
    assert len(investigator["sources"]) == 1
    assert len(verifier["sources"]) == 2
    assert verifier["near_miss_id"] == "runbook:docs-quartz-backup"
    result = json.loads(rows(cfg)[0]["result"])
    assert result["provenance"] == "ai_verified"
    assert result["route_ids"] == ["runbook:docs-quartz-restart"]
    assert result["route_ids"][0] not in json.loads(rows(cfg)[0]["baseline_ids"])
    assert capture(cfg, session_id="later", task_id="task-2")["observation"] == "would_reuse"
    report = shadow.report(cfg)
    assert report["cases"] == {"ai_verified": 1}
    assert report["counters"]["would_reuse"] == 1
    assert report["model_calls"] == 3
    assert report["usage"] == {"input_tokens": 30, "output_tokens": 15}
    assert "Quartz" not in json.dumps(report)
    assert "MUST NOT PERSIST" not in repr(rows(cfg))
    assert "Restart Quartz safely" not in repr(rows(cfg))
    assert hashlib.sha256((cfg.state_dir / "index.sqlite").read_bytes()).hexdigest() == before
    assert not (cfg.state_dir / "usage.sqlite").exists()
    assert shadow._path(cfg).is_file()
    # Windows stat modes do not represent its ACLs; retain owner RW checks there.
    mode = shadow._path(cfg).stat().st_mode & 0o777
    if os.name == "nt":
        assert mode & 0o600 == 0o600
    else:
        assert mode == 0o600


def test_only_closed_sessions_claimable_and_duplicates_preserve_readiness(cfg: Config) -> None:
    first = capture(cfg)
    assert not shadow.has_work(cfg)
    assert shadow.run_batch(cfg, llm=Model())["claimed"] == 0
    capture(cfg, session_id="session-2", task_id="task-2")
    shadow.finish_session(cfg, "session-2")
    assert shadow.has_work(cfg)
    again = capture(cfg, session_id="session-3", task_id="task-3")
    assert first["case_id"] == again["case_id"]
    assert shadow.has_work(cfg)
    assert rows(cfg)[0]["seen"] == 3


@pytest.mark.parametrize("original", ["", "continue", "Yes, do it.", "Please fix that now", "Find the same runbook"])
def test_unknown_original_request_never_inferred_from_query(cfg: Config, original: str) -> None:
    result = capture(cfg, user_request=original)
    assert result["status"] == "skipped"
    assert rows(cfg) == []


@pytest.mark.parametrize("field,value", [("task_id", "unknown"), ("turn_id", ""), ("session_id", "none"),
                                           ("user_request", "x" * 2001), ("query", "x" * 601)])
def test_missing_and_oversized_attribution_skipped(cfg: Config, field: str, value: str) -> None:
    assert capture(cfg, **{field: value})["status"] == "skipped"


def test_exact_dedup_preserves_case_punctuation_and_operation(cfg: Config) -> None:
    first = capture(cfg)
    assert capture(cfg, user_request="  Find the  Quartz service restart runbook.  ")["case_id"] == first["case_id"]
    for request in ["Find the Quartz service restart runbook!", "Find the quartz service restart runbook.",
                    "Find the Quartz service backup runbook."]:
        assert capture(cfg, user_request=request)["case_id"] != first["case_id"]
    assert capture(cfg, artifact_type="runbook")["case_id"] != first["case_id"]
    assert len(rows(cfg)) == 5


def test_shared_state_namespaced_by_profile_and_source_root(cfg: Config) -> None:
    capture(cfg)
    for changed in (replace(cfg, hermes_home=cfg.hermes_home / "other"),
                    replace(cfg, source_root=cfg.source_root / "other")):
        assert not shadow.has_work(changed)
        assert shadow.report(changed)["cases"] == {}
        capture(changed)
        assert shadow._path(changed) != shadow._path(cfg)
        assert len(rows(changed)) == 1
    assert len(rows(cfg)) == 1


@pytest.mark.parametrize("change", ["content", "delete", "age"])
def test_stale_verified_case_falls_back_then_refreshes_after_close(cfg: Config, change: str) -> None:
    assert complete(cfg)["verified"] == 1
    source = cfg.source_root / "docs" / "quartz-restart.md"
    if change == "content":
        source.write_text(source.read_text() + "New scope.\n")
    elif change == "delete":
        source.unlink()
    else:
        mutate(cfg, "UPDATE cases SET verified_at=?", (time.time() - 31 * 86400,))
    result = capture(cfg, session_id="refresh-session", task_id="refresh-task")
    assert result["observation"] == "fallback"
    assert result["reason"] == ("expired" if change == "age" else "changed_source")
    assert not shadow.has_work(cfg)
    shadow.finish_session(cfg, "session-1")
    assert not shadow.has_work(cfg)
    shadow.finish_session(cfg, "refresh-session")
    assert shadow.has_work(cfg)
    assert rows(cfg)[0]["calls"] == 0
    assert rows(cfg)[0]["total_calls"] == 3


def test_provider_failure_is_ambiguous_and_never_recharged(cfg: Config) -> None:
    class Failing:
        def complete_structured(self, **kwargs: Any) -> Any:
            raise RuntimeError("SECRET provider message")
    assert complete(cfg, Failing()) == {"claimed": 1, "verified": 0, "unresolved": 1}
    assert rows(cfg)[0]["reason"] == "interrupted_ambiguous"
    assert rows(cfg)[0]["total_calls"] == 1
    assert "SECRET" not in repr(rows(cfg))
    capture(cfg, session_id="new-session")
    shadow.finish_session(cfg, "new-session")
    assert not shadow.has_work(cfg)
    assert shadow.run_batch(cfg, llm=Model())["claimed"] == 0


def test_hard_interruption_recovered_without_replaying_provider_call(cfg: Config) -> None:
    class Interrupted:
        def complete_structured(self, **kwargs: Any) -> Any:
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        complete(cfg, Interrupted())
    assert rows(cfg)[0]["stage"] == "investigator_inflight"
    assert not shadow.has_work(cfg)
    mutate(cfg, "UPDATE cases SET lease_until=0")
    assert shadow.has_work(cfg)
    model = Model()
    assert shadow.run_batch(cfg, llm=model)["claimed"] == 0
    assert model.calls == []
    assert rows(cfg)[0]["reason"] == "interrupted_ambiguous"


def test_foreign_live_lease_and_stale_owner_cannot_publish(cfg: Config) -> None:
    capture(cfg)
    shadow.finish_session(cfg, "session-1")
    mutate(cfg, "INSERT INTO lease VALUES (1,'other',?)", (time.time() + 900,))
    assert not shadow.has_work(cfg)
    assert shadow.run_batch(cfg, llm=Model())["claimed"] == 0
    mutate(cfg, "DELETE FROM lease")

    class Stealing(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            response = super().complete_structured(**kwargs)
            mutate(cfg, "UPDATE lease SET owner='replacement'")
            return response
    assert shadow.run_batch(cfg, llm=Stealing())["error"] == "worker_error"
    assert rows(cfg)[0]["status"] == "running"
    with shadow._connect(cfg) as conn:
        assert conn.execute("SELECT owner FROM lease").fetchone()[0] == "replacement"


def test_call_budget_reserves_independent_verifier(cfg: Config) -> None:
    config = replace(cfg, verified_routing=VerifiedRoutingSettings(mode="shadow", max_model_calls_per_case=4))
    class Searching:
        calls = 0
        def complete_structured(self, **kwargs: Any) -> Any:
            self.calls += 1
            return SimpleNamespace(parsed={"action": "search", "query": "Quartz"}, usage={})
    model = Searching()
    assert complete(config, model)["unresolved"] == 1
    assert model.calls == 3
    assert rows(cfg)[0]["reason"] == "call_budget"


def test_time_budget_rejects_late_result_and_prevents_next_call(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(shadow.time, "monotonic", lambda: clock[0])
    class Late(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            response = super().complete_structured(**kwargs)
            clock[0] += 301
            return response
    model = Late()
    assert complete(cfg, model)["unresolved"] == 1
    assert len(model.calls) == 1
    assert rows(cfg)[0]["reason"] == "time_budget"
    assert json.loads(rows(cfg)[0]["usage"])["input_tokens"] == 10


@pytest.mark.parametrize("tamper", ["hash", "locator", "lines", "unread", "verdict", "near_miss"])
def test_invented_or_rejected_verification_never_becomes_verified(cfg: Config, tamper: str) -> None:
    class Tampering(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            response = super().complete_structured(**kwargs)
            if kwargs["purpose"].endswith("verifier"):
                parsed = response.parsed
                if tamper == "hash":
                    parsed["citations"][0]["sha256"] = "0" * 64
                elif tamper == "locator":
                    parsed["citations"][0]["locator"] = "/secret.md"
                elif tamper == "lines":
                    parsed["citations"][0]["end_line"] = 9999
                elif tamper == "unread":
                    parsed["citations"][0]["id"] = "doc:invented"
                elif tamper == "verdict":
                    parsed["verdict"] = "unresolved"
                else:
                    parsed["near_miss_rejected"] = False
            return response
    assert complete(cfg, Tampering())["unresolved"] == 1
    assert rows(cfg)[0]["status"] == "unresolved"
    assert capture(cfg)["observation"] == "fallback"


def test_changed_source_during_model_call_not_published(cfg: Config) -> None:
    class Changing(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            response = super().complete_structured(**kwargs)
            if kwargs["purpose"].endswith("verifier"):
                (cfg.source_root / "docs" / "quartz-restart.md").write_text("Changed operation")
            return response
    assert complete(cfg, Changing())["unresolved"] == 1
    assert rows(cfg)[0]["reason"] == "source_changed_during_verification"


def test_queue_cap_counter_only_no_unbounded_observation_rows(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shadow, "MAX_CASES", 2)
    capture(cfg)
    capture(cfg, query="Quartz second query")
    assert capture(cfg, query="Quartz third query")["reason"] == "capacity"
    for turn in range(10):
        capture(cfg, turn_id=f"distinct-{turn}")
    assert len(rows(cfg)) == 2
    assert shadow.report(cfg)["counters"]["skipped_capacity"] == 1
    assert shadow.report(cfg)["counters"]["recurrences"] == 10


def test_capture_fails_open_when_locked(cfg: Config) -> None:
    capture(cfg)
    with shadow._connect(cfg, create=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert capture(cfg)["reason"] == "capture_error"
        conn.rollback()


def test_sources_reject_unregistered_nonmarkdown_oversized_and_root_mismatch(cfg: Config) -> None:
    artifact_id = "runbook:docs-quartz-restart"
    source = shadow_sources.read_source(cfg, artifact_id)
    assert source["text"].startswith("# Quartz")
    with pytest.raises(ValueError, match="missing_source"):
        shadow_sources.read_source(cfg, "doc:not-in-index")
    with pytest.raises(ValueError, match="index_root_mismatch"):
        shadow_sources.read_source(replace(cfg, source_root=cfg.hermes_home), artifact_id)
    path = Path(source["locator"])
    path.write_bytes(b"x" * (shadow_sources.MAX_SOURCE_BYTES + 1))
    with pytest.raises(ValueError, match="source_too_large"):
        shadow_sources.read_source(cfg, artifact_id)
    with sqlite3.connect(cfg.state_dir / "index.sqlite") as conn:
        conn.execute("UPDATE artifacts SET type='script' WHERE id=?", (artifact_id,))
    with pytest.raises(ValueError, match="unsupported_source"):
        shadow_sources.read_source(cfg, artifact_id)


def test_retargeted_source_symlink_cannot_read_outside_registered_roots(cfg: Config, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("MUST NOT READ")
    path = cfg.source_root / "docs" / "quartz-restart.md"
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="unregistered_source_path"):
        shadow_sources.read_source(cfg, "runbook:docs-quartz-restart")


def test_same_turn_replay_is_not_recurrence(cfg: Config) -> None:
    capture(cfg)
    for _ in range(5):
        capture(cfg)
    assert rows(cfg)[0]["seen"] == 1
    assert shadow.report(cfg)["counters"] == {"captures": 1, "fallback": 1}
    capture(cfg, turn_id="next-turn")
    assert rows(cfg)[0]["seen"] == 2
    assert shadow.report(cfg)["counters"]["recurrences"] == 1


def test_quoted_space_and_original_text_are_preserved(cfg: Config) -> None:
    original = 'Find  the Quartz runbook for target "test  box".'
    first = capture(cfg, user_request=original)
    assert rows(cfg)[0]["user_request"] == original
    assert capture(cfg, user_request=original.replace("test  box", "test box"))["case_id"] != first["case_id"]
    assert capture(cfg, user_request=original.replace("Find  the", "Find the"))["case_id"] == first["case_id"]


def test_host_dataclass_usage_model_provider_and_elapsed_receipts(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(shadow.time, "monotonic", lambda: clock[0])
    @dataclass
    class PluginLlmUsage:
        prompt_tokens: int = 9
        completion_tokens: int = 3
        total_tokens: int = 12
        cost_usd: float = 0.001
    class HostModel(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            response = super().complete_structured(**kwargs)
            clock[0] += 0.25  # Model latency independent of native clock resolution.
            response.usage = PluginLlmUsage()
            response.model = "example-model"
            response.provider = "example-provider"
            return response
    assert complete(cfg, HostModel())["verified"] == 1
    assert shadow.report(cfg)["usage"] == {"prompt_tokens": 27, "completion_tokens": 9,
                                          "total_tokens": 36, "cost_usd": 0.003}
    assert json.loads(rows(cfg)[0]["models"]) == [{"model": "example-model", "provider": "example-provider"}]
    assert shadow.report(cfg)["elapsed_seconds"] == 0.75


def test_verified_stage_interruption_retains_proposal_without_replay(cfg: Config) -> None:
    class InterruptedVerifier(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            if kwargs["purpose"].endswith("verifier"):
                raise KeyboardInterrupt()
            return super().complete_structured(**kwargs)
    with pytest.raises(KeyboardInterrupt):
        complete(cfg, InterruptedVerifier())
    assert rows(cfg)[0]["stage"] == "verifier_inflight"
    assert json.loads(rows(cfg)[0]["result"])["provenance"] == "ai_proposed"
    mutate(cfg, "UPDATE cases SET lease_until=0")
    assert shadow.run_batch(cfg, llm=Model())["claimed"] == 0
    assert rows(cfg)[0]["status"] == "unresolved"
    assert json.loads(rows(cfg)[0]["result"])["route_ids"] == ["runbook:docs-quartz-restart"]
    assert rows(cfg)[0]["total_calls"] == 3


def test_investigator_can_recover_from_oversized_source_without_truncation(cfg: Config) -> None:
    huge = cfg.source_root / "docs" / "quartz-huge.md"
    huge.write_text("# Quartz restart long reference\n" + "Oversized body. " * 2000)
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    class Recovering(Model):
        def complete_structured(self, **kwargs: Any) -> Any:
            packet = json.loads(kwargs["input"][0]["text"])
            if not self.calls:
                self.calls.append(kwargs)
                return SimpleNamespace(parsed={"action": "read", "ids": ["runbook:docs-quartz-huge"]}, usage={})
            assert "Oversized body" not in kwargs["input"][0]["text"]
            assert packet["read_refusals"]["runbook:docs-quartz-huge"] == "source_too_large"
            return super().complete_structured(**kwargs)
    model = Recovering()
    assert complete(cfg, model)["verified"] == 1
    assert len(model.calls) == 4


def test_artifact_filter_never_reads_another_type(cfg: Config) -> None:
    capture(cfg, artifact_type="script")
    shadow.finish_session(cfg, "session-1")
    class NoEvidence:
        def complete_structured(self, **kwargs: Any) -> Any:
            assert json.loads(kwargs["input"][0]["text"])["candidates"] == []
            return SimpleNamespace(parsed={"action": "unresolved"}, usage={})
    assert shadow.run_batch(cfg, llm=NoEvidence())["unresolved"] == 1
