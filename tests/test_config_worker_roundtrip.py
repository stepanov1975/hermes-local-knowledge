"""Regression: detached workers must read YAML emitted by the host's serializer."""
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from hermes_local_knowledge.config import _parse_local_section, resolve_config


@pytest.mark.parametrize("indentless", [True, False])
def test_worker_resolves_serialized_shadow_config(tmp_path: Path, indentless: bool) -> None:
    settings = {
        "source_root": str(tmp_path / "corpus"),
        "state_dir": str(tmp_path / "state"),
        "known_entities": ["Aster", "Quartz"],
        "runbook_dirs": ["docs", "runbooks"],
        "okf": {"enabled": False, "auto_generate": False},
        "implicit_feedback": {"enabled": False},
        "verified_routing": {"mode": "shadow", "max_cases_per_worker": 2},
    }
    text = yaml.safe_dump({"local_knowledge": settings})
    if not indentless:
        text = "\n".join("  " + line if line.startswith("  - ") else line
                         for line in text.splitlines()) + "\n"
    (tmp_path / "config.yaml").write_text(text)
    assert _parse_local_section(text) == settings
    cfg = resolve_config(tmp_path)
    assert cfg.source_root == tmp_path / "corpus"
    assert cfg.state_dir == tmp_path / "state"
    assert cfg.index_settings.known_entities == ("Aster", "Quartz")
    assert cfg.index_settings.runbook_dirs == ("docs", "runbooks")
    assert cfg.verified_routing.mode == "shadow"
    assert cfg.verified_routing.max_cases_per_worker == 2
    assert not cfg.okf.enabled and not cfg.okf.auto_generate
    assert not cfg.implicit_feedback.enabled


def test_unattached_sequence_is_not_accepted() -> None:
    with pytest.raises(ValueError):
        _parse_local_section("local_knowledge:\n  - orphan\n")
