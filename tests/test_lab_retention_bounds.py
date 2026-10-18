"""Tiny retention resource-bound regressions; all campaign data is disposable."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts import lab_retention as retention
from tests.test_lab_retention import clean, finished as _finished

finished = _finished


@pytest.mark.parametrize("limit", ["entries", "bytes"])
@pytest.mark.parametrize("interruption", ["plan", "before_result", "result"])
def test_controller_receipts_do_not_consume_candidate_capacity(
        finished, monkeypatch, limit, interruption):
    h, attempt = finished
    paths = list(attempt.rglob("*"))
    cap = len(paths) if limit == "entries" else sum(p.stat().st_size for p in paths if p.is_file())
    monkeypatch.setattr(retention, "MAX_ENTRIES" if limit == "entries" else "MAX_TREE_BYTES", cap)
    original_save = retention._save

    def save(path, value, **kwargs):
        if interruption == "before_result" and path.name == "retention-result.json":
            raise KeyboardInterrupt
        original_save(path, value, **kwargs)
        if path.name == "retention-" + interruption + ".json":
            raise KeyboardInterrupt

    def dispose(*args):
        # Isolate bookkeeping capacity from capacity freed by disposal.
        return None

    with monkeypatch.context() as patch:
        patch.setattr(retention, "_save", save)
        patch.setattr(retention, "_dispose", dispose)
        with pytest.raises(KeyboardInterrupt):
            clean(h)
    plan = (attempt / "retention-plan.json").read_bytes()
    assert (attempt / "implementation/work").is_dir()
    assert (attempt / "retention-result.json").exists() == (interruption == "result")

    assert clean(h, 200)["blocked"] == []
    assert (attempt / "retention-plan.json").read_bytes() == plan
    assert not (attempt / "implementation/work").exists()
    assert clean(h, 100 + retention.RAW_RETENTION_SECONDS)["blocked"] == []
    assert not (attempt / "implementation/private/request-1.json").exists()


@pytest.mark.parametrize("limit", ["entries", "bytes"])
@pytest.mark.parametrize("record", ["plan", "result"])
@pytest.mark.parametrize("published", [False, True])
def test_interrupted_atomic_receipt_slots_are_bounded_and_retryable(
        finished, monkeypatch, limit, record, published):
    h, attempt = finished
    paths = list(attempt.rglob("*"))
    cap = len(paths) if limit == "entries" else sum(p.stat().st_size for p in paths if p.is_file())
    monkeypatch.setattr(retention, "MAX_ENTRIES" if limit == "entries" else "MAX_TREE_BYTES", cap)
    target = attempt / ("retention-" + record + ".json")
    pending = target.with_name("." + target.name + ".pending")
    publish = "link" if record == "plan" else "replace"
    original_publish, original_unlink = getattr(os, publish), Path.unlink

    def interrupt(source, destination, *args, **kwargs):
        if Path(destination) == target:
            if published:
                original_publish(source, destination, *args, **kwargs)
            raise KeyboardInterrupt
        return original_publish(source, destination, *args, **kwargs)

    def retain_pending(path, *args, **kwargs):
        if path == pending:
            raise KeyboardInterrupt  # Model process death, bypassing finally cleanup.
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(retention, "_dispose", lambda *args: None)
        patch.setattr(os, publish, interrupt)
        patch.setattr(Path, "unlink", retain_pending)
        with pytest.raises(KeyboardInterrupt):
            clean(h)
    assert target.exists() == published
    assert pending.exists() == (not published or record == "plan")
    if not published:
        pending.write_bytes(b"{")  # A staging record may be a partial JSON write.
    before = (attempt / "retention-plan.json").read_bytes() if (attempt / "retention-plan.json").exists() else None
    assert clean(h, 200)["blocked"] == []
    if before is not None:
        assert (attempt / "retention-plan.json").read_bytes() == before
    assert not (attempt / "implementation/work").exists()
    assert clean(h, 100 + retention.RAW_RETENTION_SECONDS + 200)["blocked"] == []


@pytest.mark.parametrize("limit", ["entries", "bytes"])
@pytest.mark.parametrize("name", ["copy-retention-plan.json", ".retention-plan.json.unknown",
                                 "implementation/private/retention-result.json"])
def test_unknown_and_candidate_bookkeeping_names_remain_budgeted(finished, monkeypatch, limit, name):
    h, attempt = finished
    with monkeypatch.context() as patch:
        patch.setattr(retention, "_dispose", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            clean(h)
    paths = [p for p in attempt.rglob("*") if p.name not in retention.BOOKKEEPING_NAMES]
    cap = len(paths) if limit == "entries" else sum(p.stat().st_size for p in paths if p.is_file())
    monkeypatch.setattr(retention, "MAX_ENTRIES" if limit == "entries" else "MAX_TREE_BYTES", cap)
    (attempt / name).write_bytes(b"untrusted copy")
    result = clean(h)
    assert result["blocked"][0]["reason"] == "retention_" + ("entry" if limit == "entries" else "byte") + "_budget_exceeded"
    assert (attempt / "implementation/work").exists()


@pytest.mark.parametrize("name", sorted(retention.BOOKKEEPING_NAMES))
@pytest.mark.parametrize("kind", ["sparse", "symlink", "directory", "fifo"])
def test_bookkeeping_slots_keep_finite_regular_file_bounds(finished, monkeypatch, name, kind):
    h, attempt = finished
    path = attempt / name
    if kind == "sparse":
        with path.open("wb") as stream:
            stream.truncate(1024 * 1024)
        monkeypatch.setattr(retention, "MAX_FILE_BYTES", 512 * 1024)
    elif kind == "symlink":
        path.symlink_to(attempt / "report.json")
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    original = os.open

    def guarded(name, *args, **kwargs):
        assert Path(name) != path, "invalid bookkeeping slot was opened"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    result = clean(h)
    assert result["blocked"][0]["reason"] == (
        "retention_file_budget_exceeded" if kind == "sparse" else "nonregular_record")
    assert (attempt / "implementation/work").exists()


def test_stale_staging_hardlink_is_unlinked_not_truncated(finished):
    h, attempt = finished
    protected = attempt / "report.json"
    before, identity = protected.read_bytes(), protected.stat().st_ino
    pending = attempt / ".retention-plan.json.pending"
    pending.hardlink_to(protected)
    assert clean(h)["blocked"] == []
    assert protected.read_bytes() == before and protected.stat().st_ino == identity
    assert not pending.exists()


def test_plan_size_rejected_before_publication(finished, monkeypatch):
    h, attempt = finished
    # Make the derived plan larger than every admitted input, independently of
    # temporary-root path lengths or platform metadata serialization.
    largest = max(p.stat().st_size for p in attempt.rglob("*") if p.is_file())
    assert largest <= 64 * 1024, "keep this synthetic size-bound fixture small"
    for number in range(largest // 128 + 1):
        (attempt / "implementation/scratch" / (f"plan-{number:05d}-" + "x" * 128)).touch()
    monkeypatch.setattr(retention, "MAX_FILE_BYTES", largest)
    result = clean(h)
    assert result["blocked"][0]["reason"] == "retention_file_budget_exceeded"
    assert not (attempt / "retention-plan.json").exists()
    assert not (attempt / ".retention-plan.json.pending").exists()
    assert (attempt / "implementation/work").exists()


def test_saved_plan_read_still_checks_identity(finished, monkeypatch):
    h, attempt = finished
    assert clean(h)["blocked"] == []
    plan = attempt / "retention-plan.json"
    identity = plan.stat().st_ino
    original_read = os.read

    def mutate(fd, size):
        chunk = original_read(fd, size)
        if os.fstat(fd).st_ino == identity:
            with plan.open("ab") as stream:
                stream.write(b" ")
        return chunk

    monkeypatch.setattr(os, "read", mutate)
    assert clean(h, 100 + retention.RAW_RETENTION_SECONDS)["blocked"][0]["reason"] == "retention_read_drift"
    assert (attempt / "implementation/private/request-1.json").exists()


@pytest.mark.parametrize("location", ["implementation/scratch/large", "evaluation-candidate/output/large",
                                     "evaluation-candidate/transport.stdout"])
@pytest.mark.parametrize("saved", [False, True])
def test_sparse_files_block_before_open(finished, monkeypatch, location, saved):
    h, attempt = finished
    path = attempt / location
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"original")
    if saved:
        assert clean(h)["blocked"] == []
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(1024 * 1024)
    monkeypatch.setattr(retention, "MAX_FILE_BYTES", 512 * 1024)
    original = os.open

    def guarded(name, *args, **kwargs):
        assert Path(name) != path, "oversize sparse file was opened"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    result = clean(h, 100 + retention.RAW_RETENTION_SECONDS)
    assert result["blocked"][0]["reason"] == "retention_file_budget_exceeded"
    assert path.exists()
    assert (attempt / "implementation/private/request-1.json").exists()
    if not saved:
        assert not (attempt / "retention-plan.json").exists()


@pytest.mark.parametrize("location", ["implementation/scratch", "evaluation-candidate/output"])
@pytest.mark.parametrize("saved", [False, True])
def test_streaming_fanout_stops_at_entry_cap(finished, monkeypatch, location, saved):
    h, attempt = finished
    if saved:
        assert clean(h)["blocked"] == []
    root = attempt / location
    root.mkdir(parents=True, exist_ok=True)
    bookkeeping = {attempt / name for name in retention.BOOKKEEPING_NAMES}
    cap = sum(1 for p in attempt.rglob("*") if p not in bookkeeping) + 8
    for number in range(30):
        (root / str(number)).touch()
    monkeypatch.setattr(retention, "MAX_ENTRIES", cap)
    original = os.scandir
    seen = []
    controller_seen = []
    attempt_identity = attempt.stat().st_ino

    class Guarded:
        def __init__(self, path):
            self.stream = original(path)
            self.attempt_root = os.fstat(path).st_ino == attempt_identity

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            row = next(self.stream)
            if self.attempt_root and row.name in retention.BOOKKEEPING_NAMES:
                controller_seen.append(row.name)
                assert len(controller_seen) <= len(retention.BOOKKEEPING_NAMES)
            else:
                seen.append(row.name)
            assert len(seen) <= cap + 1, "unbounded enumeration before rejection"
            return row

    monkeypatch.setattr(os, "scandir", Guarded)
    result = clean(h)
    assert result["blocked"][0]["reason"] == "retention_entry_budget_exceeded"
    assert len(seen) <= cap + 1
    assert (attempt / "implementation/private/request-1.json").exists()


@pytest.mark.parametrize("location", ["implementation/scratch", "evaluation-candidate/output"])
def test_depth_blocker_precedes_deep_descent(finished, monkeypatch, location):
    h, attempt = finished
    root = attempt / location
    leaf = root.joinpath(*(["nested"] * 8))
    leaf.mkdir(parents=True)
    monkeypatch.setattr(retention, "MAX_DEPTH", 4)
    original = os.scandir
    directory_paths = {}
    for directory in (attempt, *attempt.rglob("*")):
        if directory.is_dir():
            info = directory.stat()
            directory_paths[(info.st_dev, info.st_ino)] = directory

    def guarded(path):
        # Attribute the actual descriptor by kernel identity, not procfs paths.
        if isinstance(path, int):
            info = os.fstat(path)
            actual = directory_paths[(info.st_dev, info.st_ino)]
        else:
            actual = Path(path)
        assert len(actual.relative_to(attempt).parts) <= 4
        return original(path)

    monkeypatch.setattr(os, "scandir", guarded)
    assert clean(h)["blocked"][0]["reason"] == "retention_depth_budget_exceeded"
    assert leaf.exists()
    assert (attempt / "candidate.index").exists()


def test_aggregate_logical_budget_blocks_without_content_reads(finished, monkeypatch):
    h, attempt = finished
    root = attempt / "evaluation-candidate/output"
    root.mkdir(parents=True)
    for number in range(3):
        with (root / str(number)).open("wb") as stream:
            stream.truncate(256 * 1024)
    monkeypatch.setattr(retention, "MAX_TREE_BYTES", 512 * 1024)
    original = os.read
    attempt_files = [p.stat() for p in attempt.rglob("*") if p.is_file()]
    attempt_identities = {(info.st_dev, info.st_ino) for info in attempt_files}

    def guarded(fd, size):
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) not in attempt_identities
        return original(fd, size)

    monkeypatch.setattr(os, "read", guarded)
    assert clean(h)["blocked"][0]["reason"] == "retention_byte_budget_exceeded"
    assert (attempt / "candidate.index").exists()


def test_settlement_candidate_json_is_bounded_before_open(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    (attempt / "resources").mkdir(parents=True)
    (attempt / "attempt.json").write_text("{}")
    (attempt / "checkpoint.json").write_text("{}")
    private = attempt / "implementation/private"
    private.mkdir(parents=True)
    path = private / "unknown.json"
    with path.open("wb") as stream:
        stream.truncate(1024 * 1024)
    monkeypatch.setattr(retention, "MAX_FILE_BYTES", 512 * 1024)
    original = os.open

    def guarded(name, *args, **kwargs):
        assert Path(name) != path, "oversize settlement evidence opened"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    with pytest.raises(retention.RetentionBlocked, match="retention_file_budget_exceeded"):
        retention.settlement_evidence(attempt)


@pytest.mark.parametrize("location", ["implementation/scratch/change", "evaluation-candidate/output/change",
                                     "evaluation-candidate/transport.stdout"])
@pytest.mark.parametrize("change", ["growth", "truncate", "replacement", "fifo"])
def test_candidate_changes_during_hash_block_without_unbounded_read(finished, monkeypatch, location, change):
    h, attempt = finished
    path = attempt / location
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"original bytes")
    original = os.read
    reads = []
    target = path.stat()
    target_identity = (target.st_dev, target.st_ino)

    def mutate(fd, size):
        chunk = original(fd, size)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == target_identity:
            reads.append(len(chunk))
            assert len(reads) == 1
            if change == "growth":
                with path.open("ab") as stream:
                    stream.write(b"growing" * 100)
            elif change == "truncate":
                path.write_bytes(b"short")
            else:
                path.rename(path.with_name(path.name + "-original"))
                if change == "fifo":
                    os.mkfifo(path)
                else:
                    path.write_bytes(b"original bytes")
        return chunk

    monkeypatch.setattr(os, "read", mutate)
    result = clean(h)
    assert result["blocked"][0]["reason"] == "retention_read_drift"
    assert reads == [len(b"original bytes")]
    assert not (attempt / "retention-plan.json").exists()
    assert (attempt / "candidate.index").exists()


def test_open_replacement_is_rejected_before_read(tmp_path, monkeypatch):
    path = tmp_path / "record"
    path.write_bytes(b"original")
    original = os.open

    def replace(name, *args, **kwargs):
        if Path(name) == path:
            path.rename(path.with_name("original"))
            os.mkfifo(path)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace)
    monkeypatch.setattr(os, "read", lambda *_: pytest.fail("replaced FIFO was read"))
    with pytest.raises(retention.RetentionBlocked, match="retention_read_drift"):
        retention._digest(path)


def test_aggregate_hash_budget_applies_without_preflight(tmp_path, monkeypatch):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_bytes(b"four")
    monkeypatch.setattr(retention, "MAX_TREE_BYTES", 8)
    reads = []
    original = os.read

    def record(fd, size):
        chunk = original(fd, size)
        reads.append(len(chunk))
        return chunk

    monkeypatch.setattr(os, "read", record)
    with pytest.raises(retention.RetentionBlocked, match="retention_byte_budget_exceeded"):
        retention._inventory(tmp_path, ["a", "b", "c"])
    assert reads == [4, 4]


def test_retention_accepts_supported_raw_stream_size(finished):
    from scripts.lab_process_output import COMMAND_OUTPUT_BYTES

    h, attempt = finished
    path = attempt / "evaluation-candidate/transport.stdout"
    path.parent.mkdir()
    # Real supported 64 MiB logical stream, still far below the retention ceiling.
    with path.open("wb") as stream:
        stream.truncate(COMMAND_OUTPUT_BYTES)
    assert clean(h)["blocked"] == []
    before = (attempt / "retention-plan.json").read_bytes()
    assert clean(h, 100 + retention.RAW_RETENTION_SECONDS)["blocked"] == []
    assert not path.exists()
    assert (attempt / "retention-plan.json").read_bytes() == before
