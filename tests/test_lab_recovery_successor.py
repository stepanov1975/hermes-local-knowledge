"""Offline real-adapter dispatch/reconciliation and bounded reviewer handoff."""
import json
import sys

import pytest

if sys.platform != "linux":
    pytest.skip("Recovery controls require Linux flock and directory fsync", allow_module_level=True)

import fcntl  # noqa: E402
from scripts import lab_agent as agent  # noqa: E402
from scripts import lab_agent_model as model  # noqa: E402
from scripts import lab_disk_guard as disk  # noqa: E402
from scripts import lab_experiment as lab  # noqa: E402
from scripts import lab_recovery as recovery  # noqa: E402
from tests.test_lab_agent import FINAL, spec_for  # noqa: E402
from tests.test_lab_experiment import Harness  # noqa: E402


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


@pytest.fixture
def provider(h, monkeypatch):
    """Real official adapter and sender; only HTTP/auth and Docker are offline fakes."""
    import httpx
    import openai
    from agent import auxiliary_client  # type: ignore[import-untyped]
    from hermes_cli import auth  # type: ignore[import-untyped]

    root = h.root / "sender"
    (root / "lab-auth").mkdir(parents=True)
    monkeypatch.setattr(model, "LAB_ROOT", str(root))
    monkeypatch.setattr(model, "check_environment", lambda: None)
    monkeypatch.setattr(auth, "resolve_codex_runtime_credentials", lambda **kw: {})
    monkeypatch.setattr(model.importlib.metadata, "version", lambda name: "0.19.0")
    state = {"calls": [], "original": None, "paths": [], "failure": "lost", "removed": []}
    def handle(request):
        private = state["paths"][-1]
        assert (private / "wire-1.json").is_file()
        state["calls"].append(request)
        # Provider produces the original response before transport loses it.
        original = {"message": {"role": "assistant", "content": json.dumps(FINAL), "tool_calls": []},
                    "usage": {"input_tokens": 11, "output_tokens": 5},
                    "wire": lab.read_json(private / "wire-1.json")}
        state["original"] = original
        raise httpx.ReadError("synthetic accepted then lost", request=request)
    real = openai.OpenAI(api_key="mock", base_url=model.ENDPOINT.rsplit("/", 1)[0], max_retries=0,
                         http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    wrapper = auxiliary_client.CodexAuxiliaryClient(real, model.MODEL)
    monkeypatch.setattr(auxiliary_client, "resolve_provider_client", lambda **kw: (wrapper, model.MODEL))
    monkeypatch.setattr(agent, "ownership", lambda *args: None)
    real_run = agent.run
    def run(args):
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": agent.IMAGE, "Config": {"Labels": {
                "org.opencontainers.image.revision": agent.SOURCE}}}])
        if args[:2] == ["docker", "rm"]:
            state["removed"].append(args[-1])
            return ""
        if args[:2] == ["docker", "ps"]:
            return ""
        if args[0] == "docker":
            return "synthetic-id"
        return real_run(args)
    monkeypatch.setattr(agent, "run", run)

    monkeypatch.setattr(recovery, "docker_inspect", lambda name: None)
    def session(**kwargs):
        if kwargs["role"] != state.get("role", "implementation"):
            return h.session(**kwargs)
        private = kwargs["attempt"] / "private"
        state["paths"].append(private)
        monkeypatch.setattr(agent, "inspect", lambda name: spec_for(
            name, agent.mounts(kwargs["attempt"], kwargs["corpus"], kwargs["role"], kwargs.get("evidence") is not None)))
        return agent.run_session(**kwargs)
    state["session"] = session
    state["root"] = root
    yield state
    real.close()


def run_real(h, provider):
    return lab.run_experiment(h.campaign, session=provider["session"], evaluator=h.evaluator,
                              checks=h.checks, loader=h.loader)


@pytest.mark.parametrize("role", ["implementation", "review"])
def test_real_modelpaused_recovers_original_receipt_without_replay(h, provider, role):
    from scripts import lab_automation as automation
    from scripts import lab_retention as retention
    provider["role"] = role
    with pytest.raises(model.ModelPaused):
        run_real(h, provider)
    pending = h.manifest()["pending"]
    assert pending and h.manifest()["pause_source"] == "recovery"
    assert len(provider["calls"]) == 1
    calls, evaluations = len(h.calls), len(h.evaluations)
    with pytest.raises(ValueError, match="pending"):
        automation.acknowledge_blockers(h.campaign)
    with pytest.raises(ValueError, match="model_completion_unknown"):
        h.run(recover_only=True)
    private = provider["paths"][-1]
    assert not (private / "response-1.json").exists()
    # Restore exactly what this offline provider produced, never manufacture an answer.
    attempt = private.parent.parent
    original_report = (attempt / "report.json").read_bytes()
    original_pointer = h.manifest()["reports"][-1].copy()
    model.save(private / "response-1.json", provider["original"])
    report = h.run(recover_only=True)
    assert report["attempt_id"] == pending and report["outcome"] == "error"
    assert h.manifest()["pending"] is None and not h.manifest()["paused"]
    assert len(provider["calls"]) == 1 and len(h.calls) == calls and len(h.evaluations) == evaluations
    assert h.manifest()["incumbent"] == h.initial
    assert report["reconciliation_required"] is True
    assert (attempt / "report.json").read_bytes() == original_report
    pointer = h.manifest()["reports"][-1]
    assert {k: pointer[k] for k in original_pointer} == original_pointer
    receipt = lab.read_json(attempt / "recovery-report.json")
    assert pointer["recovery_sha256"] == lab.file_sha(attempt / "recovery-report.json")
    assert receipt["report_sha256"] == original_pointer["sha256"]
    assert receipt["evidence"] == retention.settlement_evidence(attempt)
    automation.initialize(h.campaign)
    admitted = []
    def action(campaign, **kwargs):
        assert campaign == h.campaign and kwargs == {"recover_only": False}
        admitted.append(True)
        return {"outcome": "no_change"}  # Admission sentinel, never another session.
    assert automation.tick(h.campaign, now=3600, action=action)["state"] == "completed"
    assert admitted == [True]
    assert automation.status(h.campaign)["retention"]["blocked"] == []
    assert not automation.status(h.campaign)["failure"]
    plan = lab.read_json(attempt / "retention-plan.json")
    assert plan["recovery_sha256"] == pointer["recovery_sha256"]
    assert plan["completion"] == "settled"
    assert not (attempt / role / "work").exists()
    assert (private / "response-1.json").is_file()
    automation.manual_pause(h.campaign, True)
    assert automation.tick(h.campaign, now=3600 + retention.RAW_RETENTION_SECONDS,
                           action=action)["state"] == "paused"
    assert not (private / "response-1.json").exists()
    assert (attempt / "report.json").read_bytes() == original_report
    assert lab.read_json(attempt / "recovery-report.json") == receipt
    assert lab.read_json(attempt / "retention-plan.json") == plan
    assert admitted == [True] and len(provider["calls"]) == 1
    assert len(h.calls) == calls and len(h.evaluations) == evaluations
    assert automation.tick(h.campaign, now=3601 + retention.RAW_RETENTION_SECONDS,
                           action=action)["state"] == "paused"
    assert automation.status(h.campaign)["retention"]["blocked"] == []


@pytest.mark.parametrize("fault", [
    "missing_hash", "tampered", "legacy", "unsuccessful", "attempt", "report", "config",
    "completion", "evidence", "resources", "unanswered_wire", "response_drift",
    "new_local_intent", "resource_receipt_drift", "report_drift", "symlink",
])
def test_recovery_receipt_is_not_reusable_retention_permission(h, provider, fault):
    from scripts import lab_automation as automation

    with pytest.raises(model.ModelPaused):
        run_real(h, provider)
    private = provider["paths"][-1]
    attempt = private.parent.parent
    model.save(private / "response-1.json", provider["original"])
    h.run(recover_only=True)
    path = attempt / "recovery-report.json"
    receipt = lab.read_json(path)
    manifest = h.manifest()
    pointer = manifest["reports"][-1]
    if fault == "missing_hash":
        pointer.pop("recovery_sha256")
    elif fault == "tampered":
        lab.durable_json(path, {**receipt, "state": "tampered"}, replace=True)
    elif fault == "unanswered_wire":
        model.save(private / "request-2.json", {})
        model.save(private / "wire-2.json", {})
    elif fault == "response_drift":
        lab.durable_json(private / "response-1.json", {}, replace=True)
    elif fault == "new_local_intent":
        model.save(private / "tool-intent-1-0.json", {})
    elif fault == "resource_receipt_drift":
        record = next((attempt / "resources").glob("*.json"))
        value = lab.read_json(record)
        lab.durable_json(record, {**value, "new": "unproven"}, replace=True)
    elif fault == "report_drift":
        lab.durable_json(attempt / "report.json", {}, replace=True)
    elif fault == "symlink":
        path.rename(attempt / "original-recovery.json")
        path.symlink_to(attempt / "original-recovery.json")
    else:
        if fault == "legacy":
            receipt.pop("schema")
        elif fault == "unsuccessful":
            receipt["state"] = "quiescent"
        else:
            key, value = {
                "attempt": ("attempt_id", "other-attempt"), "report": ("report_sha256", "f" * 64),
                "config": ("config_sha256", "f" * 64), "completion": ("model_completion", "unknown"),
                "evidence": ("evidence", {}), "resources": ("resources", []),
            }[fault]
            receipt[key] = value
        lab.durable_json(path, receipt, replace=True)
        # Even rebinding a semantically invalid receipt must not grant disposal.
        pointer["recovery_sha256"] = lab.file_sha(path)
    lab.durable_json(h.campaign / "manifest.json", manifest, replace=True)
    automation.initialize(h.campaign)
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid recovery evidence must block admission")
    if fault == "report_drift":
        with pytest.raises(ValueError, match="Authoritative report drift"):
            automation.tick(h.campaign, now=3600, action=forbidden)
    else:
        assert automation.tick(h.campaign, now=3600, action=forbidden)["state"] == "paused"
    state = automation.status(h.campaign)
    assert state["retention"]["blocked"] and state["failure"]["kind"] == "retention_blocked"
    assert state["admission"] == {} and len(provider["calls"]) == 1
    assert not (attempt / "retention-plan.json").exists()
    assert (attempt / "implementation/work").exists()
    assert (private / "response-1.json").exists()


@pytest.mark.parametrize("expired", [False, True])
def test_reconciled_retention_plan_does_not_hide_later_unanswered_wire(h, provider, expired):
    from scripts import lab_retention as retention

    with pytest.raises(model.ModelPaused):
        run_real(h, provider)
    private = provider["paths"][-1]
    model.save(private / "response-1.json", provider["original"])
    h.run(recover_only=True)
    assert retention.cleanup_campaign(h.campaign, now=100)["blocked"] == []
    model.save(private / "request-2.json", {})
    model.save(private / "wire-2.json", {})
    now = 100 + retention.RAW_RETENTION_SECONDS if expired else 101
    result = retention.cleanup_campaign(h.campaign, now=now)
    assert result["blocked"][0]["reason"] == "recovery_settlement_drift"
    assert (private / "response-1.json").is_file()
    assert (private / "wire-2.json").is_file()
    assert len(provider["calls"]) == 1


def test_reconciled_raw_disposal_resumes_from_hash_bound_plan(h, provider, monkeypatch):
    from pathlib import Path
    from scripts import lab_retention as retention

    with pytest.raises(model.ModelPaused):
        run_real(h, provider)
    private = provider["paths"][-1]
    attempt = private.parent.parent
    model.save(private / "response-1.json", provider["original"])
    h.run(recover_only=True)
    assert retention.cleanup_campaign(h.campaign, now=100)["blocked"] == []
    plan = (attempt / "retention-plan.json").read_bytes()
    original = Path.unlink
    calls = []
    def interrupt(path, *args, **kwargs):
        if path.parent == private and path.name.startswith(("wire-", "request-", "response-")):
            calls.append(path)
            if len(calls) == 2:
                raise KeyboardInterrupt
        return original(path, *args, **kwargs)
    cutoff = 100 + retention.RAW_RETENTION_SECONDS
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupt)
        with pytest.raises(KeyboardInterrupt):
            retention.cleanup_campaign(h.campaign, now=cutoff)
    assert len(calls) == 2 and not calls[0].exists() and calls[1].exists()
    assert retention.cleanup_campaign(h.campaign, now=cutoff + 1)["blocked"] == []
    assert (attempt / "retention-plan.json").read_bytes() == plan
    assert not any(private.glob("response-*.json"))
    assert len(provider["calls"]) == 1


@pytest.mark.parametrize("failure", ["environment", "shared_lock", "disk"])
def test_real_request_only_is_pre_dispatch_not_ambiguous(h, provider, monkeypatch, failure):
    from scripts import lab_automation as automation
    from scripts import lab_retention as retention

    check_environment = model.check_environment
    save = model.save
    low = [False]
    locked = None
    if failure == "environment":
        def reject():
            raise model.ModelPaused("synthetic environment refused")
        monkeypatch.setattr(model, "check_environment", reject)
    elif failure == "shared_lock":
        locked = (provider["root"] / "lab-auth/benchmark-controller.lock").open("w")
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        monkeypatch.setattr(disk, "available_space", lambda path: {
            "available_blocks": 9 if low[0] else 20, "usable_blocks": 100})
        def save_then_pause(path, value):
            save(path, value)
            if path.name == "request-1.json":
                low[0] = True
                disk._CURRENT.get().sample()
        monkeypatch.setattr(model, "save", save_then_pause)
    try:
        with pytest.raises((model.ModelPaused, disk.LabDiskPaused)):
            run_real(h, provider)
    finally:
        if locked:
            locked.close()
    private = provider["paths"][-1]
    assert (private / "request-1.json").exists() and not (private / "wire-1.json").exists()
    assert provider["calls"] == []
    assert recovery.session_completion(private.parent.parent) == "incomplete"
    # Local cleanup plus the wire-before-HTTP guard prove no ambiguous completion.
    assert h.manifest()["pending"] is None
    assert h.last_report()["reconciliation_required"] is False
    assert not h.evaluations and h.manifest()["incumbent"] == h.initial
    assert provider["removed"] == ["synthetic-id"]
    status = lab.read_json(private / "status.json")
    assert status["cleanup_complete"] is True and status["complete"] is False

    # Repair only the failed preflight cause; never synthesize a wire/response or
    # relabel this incomplete session as a successful one.
    monkeypatch.setattr(model, "check_environment", check_environment)
    if failure == "disk":
        low[0] = False
        monkeypatch.setattr(model, "save", save)
    model.check_environment()
    automation.initialize(h.campaign)
    monkeypatch.setattr(lab, "validate_config", lambda config: h.loader())
    automation.acknowledge_blockers(h.campaign)
    admitted = []
    def action(campaign, **kwargs):
        assert campaign == h.campaign and kwargs == {"recover_only": False}
        admitted.append(True)
        return {"outcome": "no_change"}  # Admission sentinel, never another session.
    assert automation.tick(h.campaign, now=3600, action=action)["state"] == "completed"
    assert admitted == [True] and provider["calls"] == []
    assert automation.status(h.campaign)["retention"]["blocked"] == []
    attempt = private.parent.parent
    plan = lab.read_json(attempt / "retention-plan.json")
    assert plan["completion"] == "incomplete" and plan["usage"] == {}
    assert (private / "request-1.json").exists()
    assert not (private.parent / "work").exists()
    automation.manual_pause(h.campaign, True)
    assert automation.tick(h.campaign, now=3600 + retention.RAW_RETENTION_SECONDS,
                           action=action)["state"] == "paused"
    assert not (private / "request-1.json").exists()
    assert not (private / "wire-1.json").exists() and not (private / "response-1.json").exists()
    assert lab.read_json(private / "status.json") == status
    assert lab.read_json(attempt / "retention-plan.json") == plan
    assert admitted == [True] and provider["calls"] == []


@pytest.mark.parametrize("source", [None, "manual", "auth"])
def test_recovered_modelpaused_never_overrides_later_independent_pause(h, provider, source):
    with pytest.raises(model.ModelPaused):
        run_real(h, provider)
    model.save(provider["paths"][-1] / "response-1.json", provider["original"])
    manifest = {**h.manifest(), "paused": True}
    manifest.pop("pause_source", None)
    if source:
        manifest["pause_source"] = source
    lab.durable_json(h.campaign / "manifest.json", manifest, replace=True)
    with pytest.raises(ValueError, match="paused or pending"):
        h.run(recover_only=True)
    assert h.manifest() == manifest and len(provider["calls"]) == 1


@pytest.mark.parametrize("role", ["implementation", "review", "generation-1/review"])
def test_request_only_after_completed_turn_is_incomplete_not_unknown(tmp_path, role):
    private = tmp_path / role / "private"
    private.mkdir(parents=True)
    for name in ("request-1.json", "wire-1.json", "response-1.json", "request-2.json"):
        recovery.atomic_json(private / name, {})
    assert recovery.session_completion(tmp_path) == "incomplete"
    recovery.atomic_json(private / "wire-2.json", {})
    with pytest.raises(recovery.RecoveryPaused, match="model_completion_unknown"):
        recovery.session_completion(tmp_path)


def test_history_projection_omits_unbounded_and_nonprose_findings():
    projection = lab.history_projection({"independent_review": {"approved": False,
        "revision": "a" * 40, "findings": ["safe reason", "x" * 16001, {"raw": "private"},
            {"severity": "P1", "message": "Check fallback", "raw_response": "private"}],
        "raw_response": "private"}})
    assert projection["independent_review"] == {
        "approved": False, "revision": "a" * 40,
        "findings": ["safe reason", "severity: P1; message: Check fallback"]}


def test_reviewer_rejection_reaches_next_attempt_as_safe_bounded_history(h):
    original = h.session
    reason = "Check alias compatibility before revisiting this strategy"
    def session(**kwargs):
        result = original(**kwargs)
        if kwargs["role"] == "review":
            result["final"].update(approved=False, findings=[reason, "api_key=synthetic-secret-value",
                {"private_response": "must-not-propagate"}, "x" * 5000] + ["extra"] * 40)
        return result
    h.session = session
    rejected = h.run()
    assert rejected["outcome"] == "rejected"
    h.revisit_reason = "Address previous independent rejection"
    h.run()
    history = lab.read_json(h.calls[2]["history"])["reports"][0]
    review = history["independent_review"]
    assert review["approved"] is False and review["revision"] == rejected["revision"]
    assert review["findings"][0] == reason
    encoded = json.dumps(review)
    assert "synthetic-secret-value" not in encoded and "must-not-propagate" not in encoded
    assert len(review["findings"]) <= 20 and all(len(s) <= 1000 for s in review["findings"])
    assert set(review) == {"approved", "revision", "findings"}
