from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.render_release_notes import (
    ReleaseNotesError,
    inspect_release,
    main,
    render_release_notes,
    verify_release_body,
    verify_release_complete,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_render_release_notes_extracts_exact_version_section_and_compare_link(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        """# Changelog

## [1.2.0] - 2026-08-07

### Added

- Added one thing.
- Added another thing.

### Fixed

- Fixed the release.

[1.2.0]: https://example.test/compare/v1.1.0...v1.2.0

## [1.1.0] - 2026-08-01

### Added

- Older item.

[1.1.0]: https://example.test/compare/v1.0.0...v1.1.0
""",
        encoding="utf-8",
    )

    assert render_release_notes(changelog, "1.2.0") == (
        "## Added\n\n"
        "- Added one thing.\n"
        "- Added another thing.\n\n"
        "## Fixed\n\n"
        "- Fixed the release.\n\n"
        "**Full changelog:** [v1.1.0...v1.2.0](https://example.test/compare/v1.1.0...v1.2.0)\n"
    )


def test_render_release_notes_rejects_missing_or_ambiguous_sections(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        """# Changelog

## [1.2.0] - 2026-08-07

### Added

- First copy.

## [1.2.0] - 2026-08-08

### Added

- Second copy.

[1.2.0]: https://example.test/compare/v1.1.0...v1.2.0
""",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseNotesError, match="exactly one changelog section"):
        render_release_notes(changelog, "1.2.0")
    with pytest.raises(ReleaseNotesError, match="no changelog section"):
        render_release_notes(changelog, "9.9.9")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            """# Changelog

## [1.2.0] - 2026-08-07

[1.2.0]: https://example.test/compare/v1.1.0...v1.2.0
""",
            "is empty",
        ),
        (
            """# Changelog

## [1.2.0] - 2026-08-07

### Fixed

- Complete notes.
""",
            "compare link",
        ),
    ],
)
def test_render_release_notes_rejects_empty_sections_and_missing_compare_links(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(body, encoding="utf-8")
    with pytest.raises(ReleaseNotesError, match=message):
        render_release_notes(changelog, "1.2.0")


def test_render_release_notes_extracts_last_version_section(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        """# Changelog

## [1.2.0] - 2026-08-07

### Added

- Newer item.

[1.2.0]: https://example.test/compare/v1.1.0...v1.2.0

## [1.1.0] - 2026-08-01

### Fixed

- Last section item.

[1.1.0]: https://example.test/compare/v1.0.0...v1.1.0
[1.0.0]: https://example.test/compare/v0.9.0...v1.0.0
""",
        encoding="utf-8",
    )
    rendered = render_release_notes(changelog, "1.1.0")
    assert "Last section item." in rendered
    assert "[1.1.0]:" not in rendered
    assert "[1.0.0]:" not in rendered
    assert "Newer item." not in rendered


def test_verify_release_body_requires_exact_rendered_notes(tmp_path: Path) -> None:
    expected = "## Added\n\n- Complete notes.\n"
    release_json = tmp_path / "release.json"
    release_json.write_text(json.dumps({"body": expected}), encoding="utf-8")
    verify_release_body(release_json, expected)

    release_json.write_text(json.dumps({"body": "https://example.test/compare"}), encoding="utf-8")
    with pytest.raises(ReleaseNotesError, match="does not match"):
        verify_release_body(release_json, expected)


def _release_payload(body: str) -> dict[str, object]:
    return {
        "assets": [
            {"name": "package.whl", "size": 100, "state": "uploaded"},
            {"name": "package.tar.gz", "size": 200, "state": "uploaded"},
            {"name": "package.spdx.json", "size": 50, "state": "uploaded"},
        ],
        "body": body,
        "isDraft": False,
        "isPrerelease": False,
    }


def _inspect_payload(tmp_path: Path, payload: dict[str, object], expected: str) -> dict[str, bool]:
    release_json = tmp_path / "release-state.json"
    release_json.write_text(json.dumps(payload), encoding="utf-8")
    return inspect_release(
        release_json,
        expected,
        expected_wheel="package.whl",
        expected_sdist="package.tar.gz",
    )


def test_release_state_complete_with_unrelated_provenance_asset_is_noop(tmp_path: Path) -> None:
    expected = "complete notes"
    state = _inspect_payload(tmp_path, _release_payload(expected), expected)
    assert state == {
        "assets_complete": True,
        "body_needs_update": False,
        "build_needed": False,
        "needed": False,
        "notes_match": True,
        "release_shape_complete": True,
    }


def test_release_state_body_only_repair_does_not_rebuild(tmp_path: Path) -> None:
    state = _inspect_payload(tmp_path, _release_payload("wrong notes"), "complete notes")
    assert state["needed"] is True
    assert state["body_needs_update"] is True
    assert state["build_needed"] is False


def test_release_state_missing_or_empty_expected_asset_requires_build(tmp_path: Path) -> None:
    payload = _release_payload("complete notes")
    assets = payload["assets"]
    assert isinstance(assets, list)
    assets[1] = {"name": "package.tar.gz", "size": 0, "state": "uploaded"}
    state = _inspect_payload(tmp_path, payload, "complete notes")
    assert state["needed"] is True
    assert state["build_needed"] is True


def test_release_state_draft_only_repair_does_not_rebuild(tmp_path: Path) -> None:
    payload = _release_payload("complete notes")
    payload["isDraft"] = True
    state = _inspect_payload(tmp_path, payload, "complete notes")
    assert state["needed"] is True
    assert state["build_needed"] is False
    assert state["release_shape_complete"] is False


def test_verify_complete_release_accepts_unrelated_assets_and_rejects_missing_expected(
    tmp_path: Path,
) -> None:
    expected = "complete notes"
    release_json = tmp_path / "release-complete.json"
    payload = _release_payload(expected)
    release_json.write_text(json.dumps(payload), encoding="utf-8")
    verify_release_complete(
        release_json,
        expected,
        expected_wheel="package.whl",
        expected_sdist="package.tar.gz",
    )

    assets = payload["assets"]
    assert isinstance(assets, list)
    del assets[1]
    release_json.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseNotesError, match="assets_complete"):
        verify_release_complete(
            release_json,
            expected,
            expected_wheel="package.whl",
            expected_sdist="package.tar.gz",
        )


def test_release_notes_cli_emits_executable_state_classification(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = render_release_notes(REPO_ROOT / "CHANGELOG.md", "0.4.4")
    release_json = tmp_path / "release-cli.json"
    release_json.write_text(json.dumps(_release_payload(expected)), encoding="utf-8")
    output = tmp_path / "notes.md"

    status = main(
        [
            "--version",
            "0.4.4",
            "--changelog",
            str(REPO_ROOT / "CHANGELOG.md"),
            "--output",
            str(output),
            "--release-json",
            str(release_json),
            "--expected-wheel",
            "package.whl",
            "--expected-sdist",
            "package.tar.gz",
            "--inspect-release",
        ]
    )

    assert status == 0
    assert json.loads(capsys.readouterr().out)["needed"] is False
    assert output.read_text(encoding="utf-8") == expected


def test_release_workflow_uses_exact_changelog_notes_for_create_repair_and_verification() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "--generate-notes" not in workflow
    assert 'RELEASE_NOTES_FILE: ${{ runner.temp }}/release-notes.md' in workflow
    assert '--notes-file "$RELEASE_NOTES_FILE"' in workflow
    assert "render_release_notes.py" in workflow
    assert 'python "$RELEASE_NOTES_SCRIPT"' in workflow
    assert 'git show "${tag}:scripts/render_release_notes.py"' in workflow
    assert "steps.release.outputs.tag_exists == 'true'" in workflow
    assert '"$tag_exists" == true && "$expected_sha" != "$HEAD_SHA"' in workflow
    assert "--release-json" in workflow
    assert "--json isDraft,isPrerelease,assets,body" in workflow
    assert "steps.release.outputs.build_needed == 'true'" in workflow
    assert "--inspect-release" in workflow
    assert "--verify-complete" in workflow
    assert 'python -m pip install --requirement "$RELEASE_REQUIREMENTS_FILE"' in workflow
    assert "python -m pip install --upgrade pip build twine" not in workflow
    assert 'repos/${GITHUB_REPOSITORY}/releases/tags/${tag}' in workflow
    assert "--json isImmutable,isDraft,isPrerelease,assets,body" in workflow
    assert 'if [[ "$release_is_draft" == true ]]' in workflow
    assert 'if [[ "$expected_sha" != "$HEAD_SHA" ]]' in workflow
    assert "Draft assets are still mutable" in workflow
    draft_repair = workflow.split('if [[ "$release_is_draft" == true ]]', maxsplit=1)[1].split(
        'if [[ "$release_is_immutable" == true', maxsplit=1
    )[0]
    assert "build_needed=true" in draft_repair
    assert "needed=true" in draft_repair
    assert 'run: git checkout --detach "$EXPECTED_SHA"' in workflow
    assert workflow.count("            --draft \\\n") == 2
    assert 'gh release create "$RELEASE_TAG" dist/*' not in workflow
    assert '"dist/$EXPECTED_WHEEL"' in workflow
    assert '"dist/$EXPECTED_SDIST"' in workflow
    assert "Repair prerelease status" in workflow
    prerelease_repair = workflow.split("- name: Repair prerelease status", maxsplit=1)[1].split(
        "- name: Create GitHub release from existing tag", maxsplit=1
    )[0]
    assert "release_is_prerelease == 'true'" in prerelease_repair
    assert "release_is_draft == 'false'" not in prerelease_repair
    assert "Verify draft tag target" in workflow
    assert 'actual_sha=$(git rev-parse "${RELEASE_TAG}^{commit}")' in workflow
    assert "Verify draft release content" in workflow
    assert ".isDraft == true and .isPrerelease == false" in workflow
    assert ".assets_complete and .notes_match" in workflow
    assert "Publish verified draft release" in workflow
    assert "final verification will reject partial publication" not in workflow
    assert "gh api --method DELETE" not in workflow


def test_repository_release_notes_exclude_changelog_reference_definition() -> None:
    rendered = render_release_notes(REPO_ROOT / "CHANGELOG.md", "0.4.5")
    assert "[0.4.5]:" not in rendered
    assert "preserving the pre-0.16 default lint rule set" in rendered
    assert rendered.endswith(
        "**Full changelog:** [v0.4.4...v0.4.5]"
        "(https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.4...v0.4.5)\n"
    )
