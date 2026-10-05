from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_local_knowledge import __version__, observer, plugin, telemetry
from hermes_local_knowledge.config import Config, IndexSettings


@pytest.mark.parametrize("tool,marker", [("skill_view", "_source_path"), ("read_file", "path")])
@pytest.mark.parametrize("body_size", [1300000, 4194176], ids=["megabyte", "near-scan-limit"])
def test_large_content_metadata_only(tool: str, marker: str, body_size: int) -> None:
    # skill_view is uncapped; read_file's character cap is host configurable.
    raw = json.dumps({"success": True, marker: "/synthetic/SKILL.md",
                      "content": "private body " + "x" * (body_size - 13)})
    assert len(raw) <= observer.MAX_CONTENT_SCAN_CHARS
    if body_size == 4194176:
        assert observer.MAX_CONTENT_SCAN_CHARS - len(raw) < 128
    payload = {"tool_name": tool}
    observer.project_result(payload, raw, False)
    assert payload["status"] == "success"
    expected = {"success": True, marker: "/synthetic/SKILL.md"} if tool == "skill_view" else {"success": True, "content": ""}
    assert json.loads(payload["result"]) == expected
    assert "private body" not in json.dumps(payload)
    # Larger valid bodies do not excuse a malformed complete envelope.
    observer.project_result(payload, raw[:-1], False)
    assert payload["status"] == "unknown"


def test_durable_diagnostics_scope_and_event_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(tmp_path / "source", tmp_path / "profile", tmp_path / "state", IndexSettings())
    monkeypatch.setattr(observer, "resolve_config", lambda: cfg)
    monkeypatch.setattr(observer.okf, "_tool_metadata", lambda name: ("synthetic", {}))
    queue = observer.Observer(lambda kind, **payload: None)
    try:
        queue.middleware({}, lambda args: '{"success":true}', tool_name="synthetic", task_id="task")
        assert queue.drain(5)
    finally:
        assert queue.close(5)
    report = telemetry._usage_report(cfg.source_root, days=7, limit=5,
                                     usage_db_path=cfg.state_dir / "usage.sqlite")
    diagnostics = report["observer_diagnostics"]
    counts = diagnostics["counts"]
    for counter in ("accepted", "completed", "delivered", "tool_observed", "outcome_success",
                    "attribution_skipped", "unkeyed", "missing_session_id"):
        assert counts[counter] == 1
    assert counts.get("attributed", 0) == 0
    assert diagnostics["plugin_version"] == __version__
    assert diagnostics["coverage"] == "observed_callbacks_only"
    assert diagnostics["persistence"] == "best_effort"
    assert plugin._agent_usage_report(report)["observer_diagnostics"] == {
        "coverage": "observed_callbacks_only",
        "counts": {"tool_observed": 1, "outcome_success": 1, "attribution_skipped": 1},
    }
    with sqlite3.connect(cfg.state_dir / "usage.sqlite") as conn:
        assert "task" not in str(conn.execute("SELECT * FROM observer_counts").fetchall())
        conn.executemany(
            "INSERT INTO observer_counts VALUES (?, ?, ?, ?, ?)",
            [(str(cfg.source_root), "old-version", telemetry._utc_now(), "accepted", 200),
             (str(cfg.source_root), __version__, "2000-01-01T00:00:00Z", "accepted", 300)],
        )
    # A new report/process cannot mix roots, versions or an earlier window.
    assert telemetry._usage_report(cfg.source_root, days=7, limit=5,
                                   usage_db_path=cfg.state_dir / "usage.sqlite")["observer_diagnostics"] == diagnostics
    other = telemetry._usage_report(tmp_path / "other", days=7, limit=5,
                                    usage_db_path=cfg.state_dir / "usage.sqlite")
    assert other["observer_diagnostics"]["counts"] == {}


def test_diagnostics_do_not_block_foreground_and_drain_covers_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config(tmp_path / "source", tmp_path / "profile", tmp_path / "state", IndexSettings())
    monkeypatch.setattr(observer, "resolve_config", lambda: cfg)
    monkeypatch.setattr(observer.okf, "_tool_metadata", lambda name: ("synthetic", {}))
    entered = threading.Event()
    release = threading.Event()
    writer = telemetry.record_observer_counts
    threads: list[threading.Thread] = []

    def blocked(*args, **kwargs):
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(5)
        return writer(*args, **kwargs)

    monkeypatch.setattr(telemetry, "record_observer_counts", blocked)
    queue = observer.Observer(lambda kind, **payload: None)
    try:
        # This returns even though every diagnostic write is deliberately blocked.
        assert queue.middleware({}, lambda args: '{"success":true}', tool_name="synthetic") == '{"success":true}'
        assert entered.wait(5)
        assert not queue.drain(0.01)
        assert all(thread is not threading.current_thread() for thread in threads)
        release.set()
        assert queue.drain(5)
    finally:
        release.set()
        assert queue.close(5)


def test_diagnostics_batch_receipts_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config(tmp_path / "source", tmp_path / "profile", tmp_path / "state", IndexSettings())
    monkeypatch.setattr(observer, "resolve_config", lambda: cfg)
    observed: list[str] = []
    batches: list[dict[str, int]] = []

    def writer(root, state_dir, bucket, counts):
        # Persistence must not interrupt the already-admitted delivery batch.
        assert observed == ["first", "second", "third"]
        batches.append(counts)
        return True

    release = threading.Event()

    def consume(kind, **payload):
        assert release.wait(5)
        observed.append(payload["label"])

    monkeypatch.setattr(telemetry, "record_observer_counts", writer)
    queue = observer.Observer(consume)
    try:
        slots = [queue.reserve("end") for _ in range(3)]
        for slot, label in zip(slots, ("first", "second", "third")):
            assert slot is not None
            queue.finish(slot, {"label": label})
        release.set()
        assert queue.drain(5)
        assert len(batches) == 1
        assert batches[0]["accepted"] == batches[0]["completed"] == 3
    finally:
        release.set()
        assert queue.close(5)


def test_diagnostics_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(tmp_path / "source", tmp_path / "profile", tmp_path / "state", IndexSettings())
    monkeypatch.setattr(observer, "resolve_config", lambda: cfg)
    monkeypatch.setattr(observer.okf, "_tool_metadata", lambda name: ("synthetic", {}))
    monkeypatch.setattr(telemetry, "record_observer_counts", lambda *args, **kwargs: False)
    observed = []
    queue = observer.Observer(lambda kind, **payload: observed.append(kind))
    try:
        result = object()
        assert queue.middleware({}, lambda args: result, tool_name="synthetic") is result
        assert queue.drain(5)
        assert observed == ["post"]
        assert queue.stats()["diagnostic_write_error"] == 1
    finally:
        assert queue.close(5)


def test_durable_event_partition_and_duplicate_suppression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(tmp_path / "source", tmp_path / "profile", tmp_path / "state", IndexSettings())
    monkeypatch.setattr(observer, "resolve_config", lambda: cfg)
    monkeypatch.setattr(observer.okf, "_tool_metadata", lambda name: ("synthetic", {}))
    ids = dict(session_id="private-session", task_id="private-task", turn_id="private-turn",
               api_request_id="private-api", tool_call_id="private-call")

    def fail(kind, **payload):
        raise ValueError("private failure body")

    queue = observer.Observer(fail)
    try:
        for _ in range(2):
            queue.middleware({}, lambda args: "not JSON", tool_name="synthetic", **ids)
        assert queue.drain(5)
    finally:
        assert queue.close(5)
    report = telemetry._usage_report(cfg.source_root, days=7, limit=5,
                                     usage_db_path=cfg.state_dir / "usage.sqlite")
    counts = report["observer_diagnostics"]["counts"]
    assert counts["accepted"] == counts["completed"] == 2
    assert counts["tool_observed"] == counts["outcome_unknown"] == counts["attributed"] == 1
    assert counts["duplicate"] == counts["consumer_error"] == 1
    assert counts.get("delivered", 0) == 0
    # Projection reasons count attempts (including the duplicate); not lost events.
    assert counts["result_unknown"] == counts["result_malformed"] == 2
    with sqlite3.connect(cfg.state_dir / "usage.sqlite") as conn:
        assert "private-" not in str(conn.execute("SELECT * FROM observer_counts").fetchall())


@pytest.mark.parametrize("result", ["not JSON", json.dumps({"success": True, "padding": "x" * 70000})],
                         ids=["malformed", "metadata-budget"])
def test_receipt_diagnostics_keep_admission_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str,
) -> None:
    first = Config(tmp_path / "first", tmp_path / "home-a", tmp_path / "state-a", IndexSettings())
    second = Config(tmp_path / "second", tmp_path / "home-b", tmp_path / "state-b", IndexSettings())
    current = [first]
    monkeypatch.setattr(observer, "resolve_config", lambda: current[0])
    monkeypatch.setattr(observer.okf, "_tool_metadata", lambda name: ("synthetic", {}))
    queue = observer.Observer(lambda kind, **payload: None)

    def downstream(args):
        current[0] = second
        return result

    try:
        assert queue.middleware({}, downstream, tool_name="synthetic") == result
        assert queue.drain(5)
    finally:
        assert queue.close(5)
    report = telemetry._usage_report(first.source_root, days=7, limit=5,
                                     usage_db_path=first.state_dir / "usage.sqlite")
    counts = report["observer_diagnostics"]["counts"]
    assert counts["accepted"] == counts["completed"] == counts["tool_observed"] == 1
    assert counts["outcome_unknown"] == counts["result_unknown"] == 1
    assert counts["result_malformed" if result == "not JSON" else "result_budget"] == 1
    assert not (second.state_dir / "usage.sqlite").exists()

