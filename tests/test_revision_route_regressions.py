"""Portable relevance controls from the rejected byte-change promotion veto."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_local_knowledge.config import Config, IndexSettings
from hermes_local_knowledge.routing import ROUTING_TRACE_METADATA_KEY, RouteOutcome
from hermes_local_knowledge.service import LocalKnowledgeService


@pytest.mark.parametrize("edit", ["editorial", "current_retirement"])
def test_changed_source_can_remain_a_useful_explicit_route(tmp_path: Path, edit: str) -> None:
    # This is source-grounded synthetic intent, not human or historical telemetry:
    # the caller needs the retired-format recovery procedure, including its history.
    root, state, home = tmp_path / "source", tmp_path / "state", tmp_path / "home"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "quartz-archive-restore.md").write_text(
        "# Quartz archive restore\n"
        "Restore current-format archives; this guide cannot recover retired-format archives or explain their history.\n"
    )
    target = docs / "legacy-notes.md"
    target.write_text(
        "# Legacy notes\nQuartz archive restore uses the retired archive tool.\n"
        "This retained procedure explains the history of how retired-format archives were recovered.\n"
    )
    query = "quartz archive restore history"
    service = LocalKnowledgeService(Config(
        source_root=root, state_dir=state, hermes_home=home,
        index_settings=IndexSettings(known_entities=("Quartz",)), index_max_age_seconds=0,
    ))
    artifacts, _, _ = service.rebuild()
    target_id = next(artifact.id for artifact in artifacts if Path(artifact.path).name == target.name)
    unassisted, _ = service.search(query, limit=8)
    assert unassisted[0]["id"] != target_id
    assert target_id in [row["id"] for row in unassisted]
    feedback_id, _ = service.feedback(
        rating="useful", event_id=None, query=query, artifact_id=target_id,
        note="Useful for retained retired-format procedure and its history", context={},
    )
    endorsed_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_text(target.read_text() + (
        "\n<!-- Editorial clarification; procedure unchanged. -->\n"
        if edit == "editorial" else
        "\nThe retired tool is unavailable for current recovery; this procedure remains historical reference.\n"
    ))
    assert hashlib.sha256(target.read_bytes()).hexdigest() != endorsed_hash
    service.rebuild()
    rows, metadata = service.search(query, limit=8)
    decision = metadata[ROUTING_TRACE_METADATA_KEY].decision
    # Ensure the selected route actually changes first place; no vacuous absence win.
    assert decision.outcome == RouteOutcome.PROMOTED_EXISTING
    assert decision.feedback_id == feedback_id
    assert rows[0]["id"] == target_id
    assert root / rows[0]["path"] == target
