from __future__ import annotations

import json
from pathlib import Path

from hermes_local_knowledge import plugin


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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


def test_register_exposes_native_tools():
    calls = []

    class Ctx:
        def register_tool(self, **kwargs):
            calls.append(kwargs)

    plugin.register(Ctx())

    assert [call["name"] for call in calls] == [
        "knowledge_search",
        "knowledge_get",
        "knowledge_neighbors",
        "knowledge_feedback",
        "knowledge_usage_report",
    ]
    assert {call["toolset"] for call in calls} == {"local_knowledge"}
    assert all(call["schema"]["parameters"]["type"] == "object" for call in calls)
    assert all(call["check_fn"] is plugin.check_knowledge_available for call in calls)


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
  known_entities:
    - Paperless
""",
    )

    cfg = plugin._runtime_config()

    assert cfg.source_root == repo.resolve()
    assert cfg.state_dir == state_dir.resolve()
    assert cfg.index_settings.known_entities == ("Paperless",)


def test_missing_artifact_returns_tool_error(tmp_path, monkeypatch):
    repo, hermes_home, state_dir = make_temp_repo(tmp_path)
    configure_env(monkeypatch, repo, hermes_home, state_dir)

    payload = json.loads(plugin._handle_get({"artifact_id": "skill:nope", "rebuild": True}))

    assert payload["success"] is False
    assert "Artifact not found" in payload["error"]
    assert isinstance(payload["usage_event_id"], int)


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
    assert any(row["query"] == "zzzzzzzz unlikely" for row in report["zero_result_queries"])
    assert any(row["rating"] == "wrong_artifact" for row in report["recent_negative_feedback"])
    assert any(item["type"] == "zero_result_query" for item in report["improvement_candidates"])
    assert any(item["type"] == "feedback_wrong_artifact" for item in report["improvement_candidates"])
