"""Offline real-AIAgent smoke for an explicitly supplied unmodified host checkout.

Run with Python -S; arguments: official-source dependency-site-packages private-home.
The host source must be official v2026.9.14 (345cd2b); verify Git externally.
This script blocks socket connections and child processes, not a general sandbox.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def main() -> None:
    official, site, home = (Path(arg).resolve() for arg in sys.argv[1:])
    repo = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(official), str(repo), str(site)]
    home.mkdir(parents=True, exist_ok=True)
    os.environ.update(HOME=str(home), HERMES_HOME=str(home), XDG_CACHE_HOME=str(home / "cache"),
                      TERMINAL_CWD=str(home), HERMES_TOOL_RESULT_STORAGE_DIR=str(home / "results"))
    source = home / "source"
    (source / "docs").mkdir(parents=True, exist_ok=True)
    (source / "docs/atlas.md").write_text("# Atlas restore runbook\nRestore the Atlas backup.\n")
    (home / "config.yaml").write_text(
        f"plugins:\n  enabled: []\nlocal_knowledge:\n  source_root: {source}\n"
        "  okf:\n    auto_generate: false\n  verified_routing:\n    mode: shadow\n"
    )

    def guard(event: str, args: tuple[Any, ...]) -> None:
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
            raise RuntimeError("offline smoke forbids network/process launch")

    sys.addaudithook(guard)
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager  # type: ignore[import-not-found,import-untyped]
    from hermes_local_knowledge import __version__, plugin
    from run_agent import AIAgent  # type: ignore[import-not-found,import-untyped]

    class Context(PluginContext):
        observer: Any = None

        def register_middleware(self, name: str, callback: Any) -> Any:
            self.observer = callback.__self__
            return super().register_middleware(name, callback)

    ctx = Context(PluginManifest(name="local_knowledge", version=__version__, source="user", path=str(repo)), get_plugin_manager())
    plugin.register(ctx)
    delivered: list[dict[str, Any]] = []
    consume = ctx.observer.consume

    def record(kind: str, **payload: Any) -> None:
        if kind == "post":
            delivered.append({key: payload.get(key) for key in (
                "tool_name", "session_id", "task_id", "turn_id", "api_request_id", "tool_call_id")})
        consume(kind, **payload)

    ctx.observer.consume = record
    agent = AIAgent(model="synthetic-offline", api_key="synthetic-not-a-key", provider="custom",
                    base_url="http://127.0.0.1:9/v1", api_mode="chat_completions",
                    enabled_toolsets=["local_knowledge", "file"], session_id="offline-session",
                    skip_memory=True, skip_context_files=True, skip_background_review=True,
                    quiet_mode=True, tool_progress_mode="off", checkpoints_enabled=False)
    agent._current_turn_id = "offline-turn"
    agent._current_api_request_id = "offline-api"
    ids = dict(session_id=agent.session_id, task_id="offline-task", turn_id="offline-turn", api_request_id="offline-api")
    get_plugin_manager().invoke_hook("pre_llm_call", **ids, user_message="Locate the Atlas restore runbook.", conversation_history=[])
    try:
        for index, name in enumerate(("knowledge_search", "tool_call")):
            query = {"query": "Atlas restore runbook", "limit": 3}
            args = {"calls": [{"name": "knowledge_search", "arguments": query}]} if name == "tool_call" else query
            call = SimpleNamespace(id=f"call-{index}", type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            messages: list[Any] = []
            agent._execute_tool_calls(SimpleNamespace(tool_calls=[call], content=None), messages, ids["task_id"])
            assert json.loads(messages[-1]["content"])["success"] is True
        calls = [SimpleNamespace(id=f"parallel-{i}", type="function", function=SimpleNamespace(
            name="knowledge_search", arguments=json.dumps({"query": "Atlas restore runbook"})))
            for i in range(6)]
        messages = []
        agent._execute_tool_calls(SimpleNamespace(tool_calls=calls, content=None), messages, ids["task_id"])
        assert len(messages) == 6
        assert all(json.loads(message["content"])["success"] is True for message in messages)
        assert ctx.observer.drain(20)
        expected = [{"tool_name": "knowledge_search", **ids, "tool_call_id": call}
                    for call in ["call-0", "call-1", *[f"parallel-{i}" for i in range(6)]]]
        assert sorted(delivered, key=lambda item: item["tool_call_id"]) == sorted(expected, key=lambda item: item["tool_call_id"])
        prefixes = ("hermes_cli", "agent", "tools", "model_tools", "hermes_constants", "run_agent", "hermes_local_knowledge")
        sources = {name: str(Path(filename).resolve()) for name, module in sys.modules.items()
                   if name.startswith(prefixes) and isinstance(filename := getattr(module, "__file__", None), str)}
        assert all(path.startswith((str(official) + "/", str(repo) + "/")) for path in sources.values())
        print(json.dumps({"deliveries": delivered, "queue": ctx.observer.stats(), "modules_checked": len(sources)}))
    finally:
        assert ctx.observer.close(20)
        agent.close()


if __name__ == "__main__":
    main()
