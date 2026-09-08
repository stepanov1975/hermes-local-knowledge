from pathlib import Path

import pytest

from hermes_local_knowledge.config import Config, IndexSettings
from hermes_local_knowledge.evaluation import load_quality_tiered_feedback_labels
from hermes_local_knowledge.service import LocalKnowledgeService
from hermes_local_knowledge.telemetry import _record_feedback, _record_usage
from scripts.compare_historical_query_versions import read_usage_corpus


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("quote_stored", [False, True])
def test_feedback_preserves_phrase_order(
    tmp_path: Path, quote: str, quote_stored: bool,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "topic.md").write_text("# Alpha beta gamma\n", encoding="utf-8")
    service = LocalKnowledgeService(Config(
        source_root=root, hermes_home=tmp_path / "home",
        state_dir=tmp_path / "state", index_settings=IndexSettings(),
    ))
    service.rebuild()
    stored_query = f"{quote}alpha beta gamma{quote}" if quote_stored else "alpha beta gamma"
    _record_feedback(
        root, rating="useful", event_id=None, query=stored_query,
        artifact_id="doc:topic", note="", context={}, usage_db_path=service.usage_db_path,
    )
    current_query = "gamma beta alpha" if quote_stored else f"{quote}gamma beta alpha{quote}"
    rows, metadata = service.search(current_query, limit=8)
    if quote_stored:
        # The unquoted search may still retrieve the artifact, but must not
        # reuse feedback for a different exact phrase.
        assert metadata["_routing_trace"].decision.feedback_id is None
    else:
        assert rows == []


@pytest.mark.parametrize("returned_ids,rejected_artifact", [([], ""), (["skill:wrong"], ""), (["skill:wrong"], "skill:wrong")])
def test_zero_result_and_query_level_corrections_are_evaluation_labels(
    tmp_path: Path, returned_ids: list[str], rejected_artifact: str,
) -> None:
    db = tmp_path / "usage.sqlite"
    target = "skill:backup"
    first = _record_usage(
        tmp_path, tool="knowledge_search", success=True, query="backup recovery",
        result_count=len(returned_ids), top_ids=returned_ids, usage_db_path=db,
    )
    negative, _ = _record_feedback(
        tmp_path, rating="missing", event_id=first, query="",
        artifact_id=rejected_artifact, note="", context={},
        expected_artifact_id=target, artifact_exists=lambda value: value == target,
        usage_db_path=db,
    )
    followup = _record_usage(
        tmp_path, tool="knowledge_search", success=True, query="backup restore guide",
        result_count=1, top_ids=[target], usage_db_path=db,
    )
    _record_feedback(
        tmp_path, rating="useful", event_id=followup, query="", artifact_id=target,
        note="", context={}, resolves_feedback_id=negative,
        artifact_exists=lambda value: value == target, usage_db_path=db,
    )
    evaluation = load_quality_tiered_feedback_labels(
        db, root=tmp_path, valid_artifact_ids={target},
    ).labels_by_tier["explicit_resolution"]
    historical = read_usage_corpus(db, tmp_path).quality_labels["explicit_resolution"]
    assert evaluation == historical == {"backup recovery": {target}}
