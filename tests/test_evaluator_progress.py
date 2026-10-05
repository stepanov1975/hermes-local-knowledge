from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import stat
from typing import Any

import pytest

from scripts import compare_historical_query_versions as compare
from scripts import evaluate_ref as evaluator


@pytest.fixture(autouse=True)
def reset_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluator, "_DIAGNOSTICS", None)
    monkeypatch.setattr(evaluator, "_PROGRESS_FILE", None)
    monkeypatch.setattr(evaluator, "_CONTEXT", {})


def test_lookup_failure_retains_private_context_without_changing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostic = tmp_path / "failures.jsonl"
    monkeypatch.setattr(evaluator, "_DIAGNOSTICS", diagnostic)
    evaluator._CONTEXT.update(stage="replay_search", case={"case_id": "case-1", "query": "private query"})

    def fail() -> Any:
        raise ValueError("private exception detail")

    result = evaluator._safe_call(fail)
    assert set(result) == {"status", "error_type", "error_sha256"}
    assert result["status"] == "error"
    assert "private" not in json.dumps(result)
    evidence = json.loads(diagnostic.read_text())
    assert evidence["context"]["case"]["case_id"] == "case-1"
    assert evidence["message"] == "private exception detail"
    assert "ValueError" in evidence["traceback"]
    if os.name == "posix":
        assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""


def test_status_is_private_and_has_only_stage_and_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    status = tmp_path / "progress.json"
    monkeypatch.setattr(evaluator, "_PROGRESS_FILE", status)
    rows = list(evaluator._observed_rows("labels", [{"query": "private query"}]))
    assert len(rows) == 1
    assert json.loads(status.read_text()) == {"stage": "labels", "completed": 1, "total": 1}
    if os.name == "posix":
        assert stat.S_IMODE(status.stat().st_mode) == 0o600
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "private query" not in streams.err


def test_fatal_evaluator_emits_single_redacted_json_and_private_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    request = tmp_path / "request.json"
    request.write_text('{"action":"evaluate"}')
    diagnostic = tmp_path / "failure.jsonl"
    status = tmp_path / "progress.json"
    code = evaluator.main(["--request", str(request), "--ref-root", str(tmp_path),
                           "--api-module", "missing_private_module",
                           "--diagnostics", str(diagnostic), "--progress-file", str(status)])
    streams = capsys.readouterr()
    assert code == 1
    assert json.loads(streams.out)["ok"] is False
    assert "missing_private_module" not in streams.out + streams.err
    assert "missing_private_module" in diagnostic.read_text()
    assert json.loads(status.read_text())["stage"] == "failed"


@pytest.fixture(params=[False, True])
def receipt_failure(request: Any, monkeypatch: pytest.MonkeyPatch) -> bool:
    original = compare.write_private_json

    def write(path: Path, payload: Any) -> None:
        if request.param and path.name.endswith(".receipt.json"):
            raise OSError("private receipt path detail")
        original(path, payload)

    monkeypatch.setattr(compare, "write_private_json", write)
    return bool(request.param)


@pytest.mark.parametrize("body", [
    "print('malformed private output')",
    "import sys; print('private crash detail', file=sys.stderr); sys.exit(9)",
])
def test_child_protocol_failures_preserve_private_streams_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    receipt_failure: bool, body: str,
) -> None:
    child = tmp_path / "child.py"
    child.write_text(body)
    monkeypatch.setattr(compare, "EVALUATOR", child)
    request_dir = tmp_path / "requests"
    with pytest.raises(RuntimeError):
        compare._invoke_evaluator(tmp_path, {"action": "evaluate"}, request_dir,
                                  api_module="unused", home=tmp_path, hermes_home=tmp_path)
    if not receipt_failure:
        receipt = json.loads(next(request_dir.glob("*.receipt.json")).read_text())
        assert receipt["returncode"] in (0, 9)
        assert Path(receipt["stdout_file"]).exists()
        assert Path(receipt["stderr_file"]).exists()
    for path in request_dir.iterdir():
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "private" not in streams.err
    assert ("receipt_write_failed" in streams.err) is receipt_failure


def test_live_child_status_visible_before_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    receipt_failure: bool,
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "import sys,json,time\n"
        "from pathlib import Path\n"
        "p=Path(sys.argv[sys.argv.index('--progress-file')+1])\n"
        "p.write_text(json.dumps({'stage':'labels','completed':2,'total':4,'query':'private query'}))\n"
        "time.sleep(5.2)\n"
        "print(json.dumps({'ok':True}))\n"
    )
    monkeypatch.setattr(compare, "EVALUATOR", child)
    children: list[Any] = []
    live_progress: list[str] = []
    original_popen = compare.subprocess.Popen

    def capture_child(*args: Any, **kwargs: Any) -> Any:
        process = original_popen(*args, **kwargs)
        children.append(process)
        return process

    def observe_progress(message: str, **kwargs: Any) -> None:
        if '"completed":2' in message:
            assert children[0].poll() is None
            live_progress.append(message)
        print(message, **kwargs)

    monkeypatch.setattr(compare.subprocess, "Popen", capture_child)
    monkeypatch.setattr(compare, "print", observe_progress, raising=False)
    result = compare._invoke_evaluator(tmp_path, {"action": "evaluate"}, tmp_path / "requests",
                                       api_module="unused", home=tmp_path, hermes_home=tmp_path)
    assert result == {"ok": True}
    assert live_progress
    streams = capsys.readouterr()
    assert '"completed":2' in streams.err
    assert '"stage":"labels"' in streams.err
    assert "private query" not in streams.err
    assert ("receipt_write_failed" in streams.err) is receipt_failure


@pytest.mark.parametrize("accepted", [False, True])
def test_temporary_failure_retained_success_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], accepted: bool,
) -> None:
    base = tmp_path / "private"
    base.mkdir()
    monkeypatch.setattr(compare, "_work_dir", lambda _args: (base, True))
    monkeypatch.setattr(compare, "compare_refs", lambda _args, _base:
                        compare.ComparisonRun(accepted, {"accepted": accepted}, {}))
    assert compare.main(["HEAD", "--usage-db", "unused", "--json"]) == (0 if accepted else 1)
    streams = capsys.readouterr()
    assert json.loads(streams.out) == {"accepted": accepted}
    assert base.exists() is not accepted
    if not accepted:
        assert str(base) in streams.err


def test_fatal_comparison_retains_traceback_without_json_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    base = tmp_path / "private"
    base.mkdir()
    monkeypatch.setattr(compare, "_work_dir", lambda _args: (base, True))

    def fail(_args: Any, _base: Any) -> Any:
        raise RuntimeError("private failure detail")

    monkeypatch.setattr(compare, "compare_refs", fail)
    assert compare.main(["HEAD", "--usage-db", "unused", "--json"]) == 2
    streams = capsys.readouterr()
    assert "private failure detail" not in streams.out + streams.err
    assert json.loads(streams.out)["error_type"] == "RuntimeError"
    assert json.loads((base / "failure.json").read_text())["message"] == "private failure detail"
    assert base.exists()


@pytest.mark.parametrize("diagnostic_error", [OSError, RuntimeError, RecursionError])
def test_diagnostic_failure_does_not_change_lookup_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    diagnostic_error: type[Exception],
) -> None:
    def fail() -> Any:
        raise ValueError("private lookup failure")

    expected = evaluator._safe_call(fail)
    monkeypatch.setattr(evaluator, "_DIAGNOSTICS", tmp_path / "failures.jsonl")

    def fail_diagnostic(*_args: Any, **_kwargs: Any) -> None:
        raise diagnostic_error("private diagnostic failure")

    monkeypatch.setattr(evaluator, "_write_failure_evidence", fail_diagnostic)
    assert evaluator._safe_call(fail) == expected
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "private" not in streams.err
    assert "diagnostic_write_failed" in streams.err


def test_unserializable_context_does_not_change_lookup_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> Any:
        raise ValueError("private lookup failure")

    expected = evaluator._safe_call(fail)
    monkeypatch.setattr(evaluator, "_DIAGNOSTICS", tmp_path / "failures.jsonl")
    evaluator._CONTEXT["case"] = object()
    assert evaluator._safe_call(fail) == expected
    assert capsys.readouterr().err == "evaluator-progress diagnostic_write_failed\n"


def test_interrupted_comparison_retains_private_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    base = tmp_path / "private"
    base.mkdir()
    evidence = base / "existing.json"
    evidence.write_text('{"private":"evidence"}')
    monkeypatch.setattr(compare, "_work_dir", lambda _args: (base, True))

    def interrupt(_args: Any, _base: Any) -> Any:
        raise KeyboardInterrupt("private interrupt detail")

    monkeypatch.setattr(compare, "compare_refs", interrupt)
    with pytest.raises(KeyboardInterrupt):
        compare.main(["HEAD", "--usage-db", "unused", "--json"])
    assert evidence.exists()
    receipt = json.loads((base / "failure.json").read_text())
    assert receipt["error_type"] == "KeyboardInterrupt"
    if os.name == "posix":
        assert stat.S_IMODE((base / "failure.json").stat().st_mode) == 0o600
    streams = capsys.readouterr()
    assert streams.out == ""
    assert str(base) in streams.err
    assert "private interrupt detail" not in streams.err


def test_interrupted_child_is_reaped_and_receipted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    receipt_failure: bool,
) -> None:
    child_script = tmp_path / "child.py"
    child_script.write_text("import time; time.sleep(60)")
    monkeypatch.setattr(compare, "EVALUATOR", child_script)
    original_wait = compare.subprocess.Popen.wait
    children: list[Any] = []

    def interrupt_wait(child: Any, timeout: Any = None) -> Any:
        if timeout == 5 and not children:
            children.append(child)
            raise KeyboardInterrupt("private interrupt detail")
        return original_wait(child, timeout=timeout)

    monkeypatch.setattr(compare.subprocess.Popen, "wait", interrupt_wait)
    request_dir = tmp_path / "requests"
    with pytest.raises(KeyboardInterrupt):
        compare._invoke_evaluator(tmp_path, {"action": "evaluate"}, request_dir,
                                  api_module="unused", home=tmp_path, hermes_home=tmp_path)
    assert len(children) == 1
    assert children[0].poll() is not None
    if not receipt_failure:
        receipt = json.loads(next(request_dir.glob("*.receipt.json")).read_text())
        assert receipt["returncode"] == children[0].returncode
        assert receipt["returncode"] != 0
        for key in ("request_file", "stdout_file", "stderr_file", "failures_file"):
            assert Path(receipt[key]).exists()
    for path in request_dir.iterdir():
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "private interrupt detail" not in streams.err
    assert ("receipt_write_failed" in streams.err) is receipt_failure


def test_startup_failure_keeps_original_redacted_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_args: Any) -> Any:
        raise ValueError("private startup detail")

    monkeypatch.setattr(compare, "_work_dir", fail)
    assert compare.main(["HEAD", "--usage-db", "unused", "--json"]) == 2
    streams = capsys.readouterr()
    assert set(json.loads(streams.out)) == {"accepted", "error_type", "error_sha256"}
    assert "private startup detail" not in streams.out + streams.err
    assert "retained" not in streams.err


def test_nonfatal_lookup_preserves_only_its_captured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostic = tmp_path / "failure.jsonl"
    monkeypatch.setattr(evaluator, "_DIAGNOSTICS", diagnostic)
    captured = io.StringIO()

    def fail() -> Any:
        print("private lookup diagnosis")
        raise ValueError("private lookup failure")

    with redirect_stdout(captured):
        print("earlier successful output")
        result = evaluator._safe_call(fail)
    assert result["status"] == "error"
    assert set(result) == {"status", "error_type", "error_sha256"}
    evidence = json.loads(diagnostic.read_text())
    assert evidence["captured_stdout"] == "private lookup diagnosis\n"
    assert "earlier successful output" not in evidence["captured_stdout"]
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "private" not in streams.err


def test_diagnostics_work_without_posix_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = tmp_path / "failure.jsonl"
    monkeypatch.setattr(evaluator, "_DIAGNOSTICS", diagnostic)
    monkeypatch.delattr(os, "fchmod", raising=False)
    evaluator._failure_evidence(ValueError("retained diagnosis"))
    assert json.loads(diagnostic.read_text())["message"] == "retained diagnosis"


@pytest.fixture
def printed_api(tmp_path: Path) -> tuple[Path, dict[str, Any], bytes]:
    # Real evaluator import and callback path; no monkeypatched child dispatch.
    printed = "private callback context –\r\npartial".encode("utf-8")
    (tmp_path / "printed_api.py").write_text(
        "import time\n"
        "def search_index(db, query, **kwargs):\n"
        f"    print({printed.decode('utf-8')!r}, end='')\n"
        "    if query == 'stall': time.sleep(60)\n"
        "    if query == 'fail': raise ValueError('private callback failure')\n"
        "    return []\n"
        "def build_index(*args): return [], []\n"
        "def get_artifact(*args): return None\n"
        "def get_neighbors(*args): return []\n"
    )
    cases = tmp_path / "cases.json"
    request = {"action": "evaluate", "case_file": str(cases),
               "full_db": str(tmp_path / "full.sqlite"),
               "synthetic_db": str(tmp_path / "synthetic.sqlite")}
    return cases, request, printed


def _printed_case(cases: Path, query: str) -> None:
    cases.write_text(json.dumps({"labels": {"positive": [{"query_id": "q", "query": query}]}}))


@pytest.mark.parametrize("query", ["ok", "fail"])
def test_real_evaluator_private_capture_keeps_json_and_nonfatal_contract(
    tmp_path: Path, printed_api: tuple[Path, dict[str, Any], bytes], query: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases, request, printed = printed_api
    _printed_case(cases, query)
    request_dir = tmp_path / "requests"
    result = compare._invoke_evaluator(tmp_path, request, request_dir,
                                       api_module="printed_api", home=tmp_path, hermes_home=tmp_path)
    receipt = json.loads(next(request_dir.glob("*.receipt.json")).read_text())
    capture = Path(receipt["captured_stdout_file"])
    assert capture.read_bytes() == printed
    assert result["captured_stdout_sha256"] == hashlib.sha256(printed).hexdigest()
    assert result["captured_stdout_bytes"] == len(printed)
    assert json.loads(Path(receipt["stdout_file"]).read_text()) == result
    outcome = result["label_search"]["q"]
    if query == "fail":
        assert set(outcome) == {"status", "error_type", "error_sha256", "duration_ms"}
        assert outcome["status"] == "error"
        evidence = json.loads(Path(receipt["failures_file"]).read_text())
        assert Path(evidence["captured_stdout_file"]) == capture
        assert evidence["message"] == "private callback failure"
    else:
        assert outcome["status"] == "ok"
    if os.name == "posix":
        assert stat.S_IMODE(capture.stat().st_mode) == 0o600
    public_json = json.dumps(result)
    assert "private callback context" not in public_json
    assert "private callback failure" not in public_json
    assert "captured_stdout_file" not in public_json
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "private" not in streams.err


def test_real_evaluator_printed_context_survives_parent_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    printed_api: tuple[Path, dict[str, Any], bytes], capsys: pytest.CaptureFixture[str],
) -> None:
    cases, request, printed = printed_api
    _printed_case(cases, "stall")
    request_dir = tmp_path / "requests"
    original_wait = compare.subprocess.Popen.wait
    children: list[Any] = []

    def interrupt_after_print(child: Any, timeout: Any = None) -> Any:
        if timeout == 5 and not children:
            children.append(child)
            deadline = compare.time.monotonic() + 10
            while compare.time.monotonic() < deadline:
                paths = list(request_dir.glob("*.failures.stdout.log"))
                if paths and paths[0].read_bytes() == printed:
                    assert child.poll() is None  # callback is still stalled
                    raise KeyboardInterrupt("private interrupt detail")
                try:
                    original_wait(child, timeout=0.02)
                except compare.subprocess.TimeoutExpired:
                    continue
                pytest.fail("evaluator exited before its stalled callback was interrupted")
            raise AssertionError("live printed capture not observed")
        return original_wait(child, timeout=timeout)

    monkeypatch.setattr(compare.subprocess.Popen, "wait", interrupt_after_print)
    with pytest.raises(KeyboardInterrupt):
        compare._invoke_evaluator(tmp_path, request, request_dir,
                                  api_module="printed_api", home=tmp_path, hermes_home=tmp_path)
    assert children[0].poll() is not None
    receipt = json.loads(next(request_dir.glob("*.receipt.json")).read_text())
    assert receipt["returncode"] == children[0].returncode != 0
    capture = Path(receipt["captured_stdout_file"])
    assert capture.read_bytes() == printed
    assert Path(receipt["stdout_file"]).read_bytes() == b""
    assert printed not in Path(receipt["stderr_file"]).read_bytes()
    if os.name == "posix":
        assert stat.S_IMODE(capture.stat().st_mode) == 0o600
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "private" not in streams.err


def test_direct_evaluator_without_diagnostics_preserves_capture_contract(
    tmp_path: Path, printed_api: tuple[Path, dict[str, Any], bytes],
) -> None:
    cases, request, printed = printed_api
    _printed_case(cases, "fail")
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    child = compare.subprocess.run(
        [compare.sys.executable, str(compare.EVALUATOR), "--request", str(request_path),
         "--ref-root", str(tmp_path), "--api-module", "printed_api"],
        cwd=tmp_path, env=compare.build_child_env(tmp_path, home=tmp_path, hermes_home=tmp_path,
                                                source_root=None, state_dir=None, explicit_root=True),
        capture_output=True, check=True,
    )
    payload = json.loads(child.stdout)
    assert payload["ok"] is True
    assert payload["label_search"]["q"]["status"] == "error"
    assert payload["captured_stdout_sha256"] == hashlib.sha256(printed).hexdigest()
    assert payload["captured_stdout_bytes"] == len(printed)
    assert b"private" not in child.stdout + child.stderr
    assert not list(tmp_path.glob("*.stdout.log"))
