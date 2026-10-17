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
    state = {"calls": [], "original": None, "paths": [], "failure": "lost"}
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
        if args[0] == "docker":
            return "synthetic-container"
        return real_run(args)
    monkeypatch.setattr(agent, "run", run)
    monkeypatch.setattr(agent, "cleanup", lambda *args: None)
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
    model.save(private / "response-1.json", provider["original"])
    report = h.run(recover_only=True)
    assert report["attempt_id"] == pending and report["outcome"] == "error"
    assert h.manifest()["pending"] is None and not h.manifest()["paused"]
    assert len(provider["calls"]) == 1 and len(h.calls) == calls and len(h.evaluations) == evaluations
    assert h.manifest()["incumbent"] == h.initial


@pytest.mark.parametrize("failure", ["environment", "shared_lock", "disk"])
def test_real_request_only_is_pre_dispatch_not_ambiguous(h, provider, monkeypatch, failure):
    locked = None
    if failure == "environment":
        def reject():
            raise model.ModelPaused("synthetic environment refused")
        monkeypatch.setattr(model, "check_environment", reject)
    elif failure == "shared_lock":
        locked = (provider["root"] / "lab-auth/benchmark-controller.lock").open("w")
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        save = model.save
        low = [False]
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
