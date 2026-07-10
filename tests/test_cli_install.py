from __future__ import annotations

import json
import os
import argparse
from pathlib import Path

import pytest

from hermes_local_knowledge import cli as lci_cli


BUNDLED_SKILL = (
    Path(lci_cli.__file__).resolve().parent
    / "skills"
    / "local-knowledge-router"
    / "SKILL.md"
)


def stdout_json(capsys) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return json.loads(capsys.readouterr().out)


def doctor_checks(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_checks = payload["checks"]
    assert isinstance(raw_checks, list)
    result: dict[str, dict[str, object]] = {}
    for check in raw_checks:
        assert isinstance(check, dict)
        result[str(check["name"])] = check
    return result


def test_install_router_skill_creates_normal_skill(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()

    status = lci_cli.main(
        ["install-router-skill", "--hermes-home", str(hermes_home), "--json"]
    )
    payload = stdout_json(capsys)
    target = hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"

    assert status == 0
    assert payload["success"] is True
    assert payload["status"] == "installed"
    assert payload["target"] == str(target.resolve())
    assert target.read_bytes() == BUNDLED_SKILL.read_bytes()


def test_install_router_skill_is_idempotent_for_current_content(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    target = hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(BUNDLED_SKILL.read_bytes())
    fixed_ns = 1_700_000_000_000_000_000
    os.utime(target, ns=(fixed_ns, fixed_ns))

    status = lci_cli.main(
        ["install-router-skill", "--hermes-home", str(hermes_home), "--json"]
    )
    payload = stdout_json(capsys)

    assert status == 0
    assert payload["status"] == "current"
    assert target.stat().st_mtime_ns == fixed_ns


def test_install_router_skill_refuses_to_overwrite_different_content(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    target = hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("user-customized skill\n", encoding="utf-8")

    status = lci_cli.main(
        ["install-router-skill", "--hermes-home", str(hermes_home), "--json"]
    )
    payload = stdout_json(capsys)

    assert status == 1
    assert payload["success"] is False
    assert payload["status"] == "conflict"
    assert payload["force_required"] is True
    assert target.read_text(encoding="utf-8") == "user-customized skill\n"


def test_install_router_skill_force_replaces_different_content(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    target = hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("user-customized skill\n", encoding="utf-8")

    status = lci_cli.main(
        [
            "install-router-skill",
            "--hermes-home",
            str(hermes_home),
            "--force",
            "--json",
        ]
    )
    payload = stdout_json(capsys)

    assert status == 0
    assert payload["success"] is True
    assert payload["status"] == "installed"
    assert payload["overwritten"] is True
    assert target.read_bytes() == BUNDLED_SKILL.read_bytes()


def test_install_router_skill_force_rejects_symlink_target(
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    target = hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"
    external = tmp_path / "external-skill.md"
    target.parent.mkdir(parents=True)
    external.write_text("external user content\n", encoding="utf-8")
    try:
        target.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    status = lci_cli.main(
        [
            "install-router-skill",
            "--hermes-home",
            str(hermes_home),
            "--force",
            "--json",
        ]
    )
    payload = stdout_json(capsys)

    assert status == 1
    assert payload["success"] is False
    assert payload["status"] == "conflict"
    assert "symbolic link" in str(payload["error"])
    assert external.read_text(encoding="utf-8") == "external user content\n"


def test_install_router_skill_reports_bundled_read_error_as_json(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    original_read_bytes = Path.read_bytes

    def fail_bundled_read(path: Path) -> bytes:
        if path == BUNDLED_SKILL:
            raise PermissionError("bundled read denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_bundled_read)

    status = lci_cli.main(
        ["install-router-skill", "--hermes-home", str(hermes_home), "--json"]
    )
    payload = stdout_json(capsys)

    assert status == 1
    assert payload["success"] is False
    assert payload["status"] == "error"
    assert "bundled read denied" in str(payload["error"])


def test_install_router_skill_reports_target_read_error_as_json(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    target = hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("existing skill\n", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def fail_target_read(path: Path) -> bytes:
        if path == target:
            raise PermissionError("target read denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_target_read)

    status = lci_cli.main(
        ["install-router-skill", "--hermes-home", str(hermes_home), "--json"]
    )
    payload = stdout_json(capsys)

    assert status == 1
    assert payload["success"] is False
    assert payload["status"] == "error"
    assert "target read denied" in str(payload["error"])


def test_doctor_warns_for_missing_router_skill_and_disabled_auto_generation(
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()

    status = lci_cli.main(["doctor", "--hermes-home", str(hermes_home), "--json"])
    payload = stdout_json(capsys)
    checks = doctor_checks(payload)

    assert status == 0
    assert payload["success"] is True
    assert checks["router_skill_installed"]["ok"] is False
    assert "install-router-skill" in str(checks["router_skill_installed"]["detail"])
    assert checks["okf_auto_generate"]["ok"] is False
    assert "additional model tokens" in str(checks["okf_auto_generate"]["detail"])


def test_doctor_accepts_current_router_skill_and_enabled_auto_generation(
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "local_knowledge:\n  okf:\n    auto_generate: true\n",
        encoding="utf-8",
    )
    assert lci_cli.main(
        ["install-router-skill", "--hermes-home", str(hermes_home), "--json"]
    ) == 0
    capsys.readouterr()

    status = lci_cli.main(["doctor", "--hermes-home", str(hermes_home), "--json"])
    payload = stdout_json(capsys)
    checks = doctor_checks(payload)

    assert status == 0
    assert checks["router_skill_installed"]["ok"] is True
    assert checks["router_skill_matches_plugin"]["ok"] is True
    assert checks["okf_auto_generate"]["ok"] is True


def test_doctor_warns_when_router_skill_differs_from_plugin(
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    target = hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("user-customized skill\n", encoding="utf-8")

    status = lci_cli.main(["doctor", "--hermes-home", str(hermes_home), "--json"])
    payload = stdout_json(capsys)
    checks = doctor_checks(payload)

    assert status == 0
    assert checks["router_skill_installed"]["ok"] is True
    assert checks["router_skill_matches_plugin"]["ok"] is False
    assert "--force" in str(checks["router_skill_matches_plugin"]["detail"])


def test_hermes_cli_adapter_installs_router_skill(
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    parser = argparse.ArgumentParser()

    lci_cli.setup_hermes_cli(parser)
    args = parser.parse_args(
        ["install-router-skill", "--hermes-home", str(hermes_home), "--json"]
    )
    status = lci_cli.handle_hermes_cli(args)
    payload = stdout_json(capsys)

    assert status == 0
    assert payload["status"] == "installed"
    assert (
        hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"
    ).read_bytes() == BUNDLED_SKILL.read_bytes()


def test_hermes_cli_adapter_runs_doctor(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    parser = argparse.ArgumentParser()

    lci_cli.setup_hermes_cli(parser)
    args = parser.parse_args(["doctor", "--hermes-home", str(hermes_home), "--json"])
    status = lci_cli.handle_hermes_cli(args)
    payload = stdout_json(capsys)

    assert status == 0
    assert payload["success"] is True
    assert "checks" in payload


def test_hermes_cli_adapter_preserves_failure_exit_status(
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    target = hermes_home / "skills" / "local-knowledge-router" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("user-customized skill\n", encoding="utf-8")
    parser = argparse.ArgumentParser()
    lci_cli.setup_hermes_cli(parser)
    args = parser.parse_args(
        ["install-router-skill", "--hermes-home", str(hermes_home), "--json"]
    )

    with pytest.raises(SystemExit) as exc_info:
        lci_cli.handle_hermes_cli(args)
    payload = stdout_json(capsys)

    assert exc_info.value.code == 1
    assert payload["status"] == "conflict"
