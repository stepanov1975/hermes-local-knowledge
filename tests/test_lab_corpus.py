"""Trusted preparation seam: no runtime service or production-state access."""
from pathlib import Path
import json

import pytest

from scripts.lab_corpus import (
    project_runtime, source_manifest, verify_source_manifest, require_reviews,
    scrub_content, freeze_corpus, verify_frozen,
)


def test_scrub_preserves_python_parsing_and_routing_without_literal_credentials():
    import ast
    raw = ('PASSWORD = "demo-credential"\n'
           'SECRET = f"opaque-{1}"\n'
           'config = {"api_key": "another-credential", "route": "backup"}\n'
           'url = "https://user:credential@example.org/api?token=hidden&route=backup"\n'
           'header = "Authorization: Bearer invisible"\n'
           'ref = os.getenv("DEMO_TOKEN")\n')
    safe, decisions = scrub_content(raw, suffix=".py")
    ast.parse(safe)
    assert "demo-credential" not in safe
    assert "another-credential" not in safe
    assert "invisible" not in safe
    assert "credential@example" not in safe
    assert "backup" in safe and 'os.getenv("DEMO_TOKEN")' in safe
    assert decisions["structural_literals"] >= 2


@pytest.mark.parametrize("raw,needle", [
    ('password = "synthetic-left" + "synthetic-right"\n', "synthetic-left"),
    ('data = {"api_key": "synthetic-left" + "synthetic-right"}\n', "synthetic-left"),
    ('client(password="synthetic-left" + "synthetic-right")\n', "synthetic-left"),
    ('password = os.getenv("TEST_PASSWORD", "synthetic-fallback")\n', "synthetic-fallback"),
    ('password = "synthetic-prefix${TEST_PASSWORD}"\n', "synthetic-prefix"),
])
def test_scrub_removes_compound_and_fallback_credential_values(raw, needle):
    import ast
    safe, _ = scrub_content(raw, suffix=".py")
    ast.parse(safe)
    assert needle not in safe
    # Noncredential routing context remains available.
    route, _ = scrub_content('route = "backup"\npassword = os.getenv("TEST_PASSWORD")\n', suffix=".py")
    assert '"backup"' in route and 'os.getenv("TEST_PASSWORD")' in route


def test_scrub_does_not_treat_a_mixed_environment_value_as_only_a_reference():
    safe, _ = scrub_content('password = "synthetic-prefix${TEST_PASSWORD}"\n')
    assert "synthetic-prefix" not in safe
    pure, _ = scrub_content('password = "${TEST_PASSWORD}"\n')
    assert "${TEST_PASSWORD}" in pure


def test_freeze_is_snapshot_not_continuing_live_review(tmp_path):
    source, scanner, state, settings = scanner_layout(tmp_path)
    (source / "scripts/demo.py").write_text('TOKEN = "private-literal"\nprint("route")')
    frozen = tmp_path / "frozen"
    report = freeze_corpus(source, scanner, state, settings, frozen)
    assert (frozen / "scanner/skills/demo").is_symlink()
    assert (frozen / "scanner/skills/demo").resolve() == frozen / "source/custom_skills/demo"
    assert "private-literal" not in (frozen / "source/scripts/demo.py").read_text()
    assert report["reviews"] and report["snapshot_semantics"] == "captured-inputs"
    (source / "scripts/demo.py").write_text("future source changes")
    verify_frozen(frozen)
    (frozen / "source/scripts/demo.py").chmod(0o600)
    (frozen / "source/scripts/demo.py").write_text("changed captured data")
    with pytest.raises(ValueError, match="integrity"):
        verify_frozen(frozen)
    # Restore directory permissions only for fixture cleanup, not acceptance.
    for path in sorted(frozen.rglob("*"), reverse=True):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)
    frozen.chmod(0o700)



def approved(_field: str, value: str) -> str:
    return value.replace("example-credential", "<redacted>")


def test_runtime_projection_keeps_scanner_evidence_but_never_inherits_activation():
    config = {"provider": "ignored", "mcp_servers": {"demo": {
        "command": "/usr/bin/demo", "args": ["--token", "example-credential", "--route", "backup"],
        "env": {"DEMO_TOKEN": "example-credential"},
        "headers": {"Authorization": "example-credential"}}}}
    cron = {"jobs": [{"id": "one", "name": "backup", "prompt": "Review backup runbook",
                     "state": "scheduled", "delivery": "ignored", "enabled": True}]}
    projected_config, projected_cron = project_runtime(config, cron, approved)
    assert projected_config == {"mcp_servers": {"demo": {
        "command": "/usr/bin/demo", "args": ["--token", "<redacted>", "--route", "backup"],
        "env": {"DEMO_TOKEN": ""}}}}
    assert projected_cron == {"jobs": [{"id": "one", "name": "backup",
                                       "prompt": "Review backup runbook", "state": "scheduled",
                                       "enabled": False}]}
    assert "example-credential" not in json.dumps([projected_config, projected_cron])


def test_runtime_projection_does_not_claim_unknown_strings_are_safe():
    with pytest.raises(ValueError, match="review"):
        project_runtime({"mcp_servers": {"demo": {"command": "unknown-command"}}}, {"jobs": []})


def test_runtime_projection_requires_review_for_positional_arguments_and_cron_prompt():
    reviewed = []

    def review(field: str, value: str) -> str:
        reviewed.append(field)
        return "approved" if value == "opaque-positional" else value

    config, cron = project_runtime({"mcp": {"servers": {"demo": {
        "args": ["opaque-positional"], "env": {"NAME": {"must": "not be traversed"}}}}}},
        {"jobs": [{"prompt": "source evidence"}]}, review)
    assert config["mcp"]["servers"]["demo"]["args"] == ["approved"]
    assert "mcp.servers.demo.args.0" in reviewed
    assert "cron.0.prompt" in reviewed
    assert not any("env.NAME" in field for field in reviewed)
    assert cron["jobs"][0]["prompt"] == "source evidence"


@pytest.mark.parametrize("payload", [{"jobs": [42]}, {"jobs": "bad"}, {"jobs": [{"prompt": ["bad"]}]}])
def test_runtime_projection_rejects_unsupported_registry_instead_of_skipping(payload):
    with pytest.raises(ValueError):
        project_runtime({}, payload, approved)


def scanner_layout(tmp_path: Path):
    source, scanner, state = (tmp_path / name for name in ("source", "scanner", "state"))
    for path in (source / "custom_skills/demo/references", source / "scripts", scanner / "skills", state / "okfs/tools"):
        path.mkdir(parents=True)
    (source / "custom_skills/demo/SKILL.md").write_text("---\nname: demo\n---\nRouting evidence")
    (source / "custom_skills/demo/references/support.md").write_text("Support evidence")
    (scanner / "skills/demo").symlink_to(source / "custom_skills/demo", target_is_directory=True)
    (source / "scripts/demo.py").write_text("print('demo')")
    (state / "okfs/tools/tool.md").write_text("Tool note evidence")
    (scanner / "config.yaml").write_text("{}")
    (scanner / "cron").mkdir()
    (scanner / "cron/jobs.json").write_text('{"jobs": []}')
    settings = {"custom_skill_dirs": ["custom_skills"], "script_dirs": ["scripts"],
                "memory_dirs": [], "runbook_dirs": [], "include_markdown_docs": True}
    return source, scanner, state, settings


def test_source_manifest_rejects_external_runtime_registry_link(tmp_path):
    source, scanner, state, settings = scanner_layout(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (scanner / "config.yaml").unlink()
    (scanner / "config.yaml").symlink_to(outside)
    with pytest.raises(ValueError, match="runtime"):
        source_manifest(source, scanner, state, settings)


def test_source_manifest_tracks_aliases_recursive_notes_and_drift(tmp_path):
    source, scanner, state, settings = scanner_layout(tmp_path)
    nested = state / "okfs/tools/nested"
    nested.mkdir()
    (nested / "second.md").write_text("second note")
    manifest = source_manifest(source, scanner, state, settings)
    assert manifest["scanner/skills/demo"]["canonical"] == "source/custom_skills/demo"
    assert "seed/okfs/tools/nested/second.md" in manifest
    verify_source_manifest(manifest, source, scanner, state, settings)
    (source / "scripts/demo.py").write_text("changed")
    with pytest.raises(ValueError, match="drift"):
        verify_source_manifest(manifest, source, scanner, state, settings)


def test_review_gate_rejects_missing_and_projected_drift(tmp_path):
    import hashlib
    path = tmp_path / "approved.md"
    path.write_text("approved safe evidence")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"source/doc.md": {"sha256": "original-private-hash"}}
    with pytest.raises(ValueError, match="Missing"):
        require_reviews(manifest, {})
    reviews = {"source/doc.md": {"original_sha256": "original-private-hash",
                                "projected_path": str(path), "projected_sha256": digest,
                                "review": "Actual content review"}}
    require_reviews(manifest, reviews)
    path.write_text("changed projected content")
    with pytest.raises(ValueError, match="drift"):
        require_reviews(manifest, reviews)


def test_projection_preserves_empty_optional_fields_and_scalar_timestamps():
    config, cron = project_runtime({"mcp_servers": {"demo": {"command": None,
                                  "args": None, "env": None}}},
                                  {"jobs": [{"id": "one", "script": None,
                                             "skills": None, "created_at": 1234.5}]}, approved)
    assert config == {"mcp_servers": {"demo": {}}}
    assert cron == {"jobs": [{"enabled": False, "id": "one", "created_at": 1234.5}]}
