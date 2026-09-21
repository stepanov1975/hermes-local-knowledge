from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import shutil
from typing import Any

import pytest

from scripts import check_version_policy as policy
from scripts.render_release_notes import main, release_metadata, release_version_key

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("base,current", [
    ("0.4.20", "0.5.3b1"), ("0.5.2", "0.5.3b1"),
    ("0.5.3b1", "0.5.3b2"), ("0.5.3b2", "0.5.3b10"),
    ("0.5.3b10", "0.5.3"), ("0.5.3", "0.5.4b1"),
])
def test_beta_ordering(base: str, current: str) -> None:
    assert policy.is_version_bumped(current=current, base=base)
    assert not policy.is_version_bumped(current=base, base=current)
    assert not policy.is_version_bumped(current=current, base=current)


@pytest.mark.parametrize("version", [
    "v0.5.3b1", "0.5.3beta1", "0.5.3b", "0.5.3rc1", "0.5.3.dev1",
    "0.5.3+local", "0.5.3post1", "0.05.3b1", "0.5.3b01", "0.5.3b1\n",
])
def test_noncanonical_or_unsupported_versions_rejected(version: str) -> None:
    with pytest.raises(ValueError, match="invalid release version"):
        release_version_key(version)
    with pytest.raises(policy.PolicyError, match="invalid release version"):
        policy.simple_version_key(version)


def test_repository_beta_versions_are_synchronized() -> None:
    assert policy.require_metadata_in_sync(policy.read_current_metadata(ROOT)) == "0.5.3b4"
    mismatched = policy.VersionMetadata("0.5.3b1", "0.5.3", "0.5.3b1")
    with pytest.raises(policy.PolicyError, match="not synchronized"):
        policy.require_metadata_in_sync(mismatched)


@pytest.mark.parametrize("version,prerelease", [("0.5.3", False), ("0.5.3b1", True)])
@pytest.mark.parametrize("actual", [False, True])
def test_cli_release_status_and_repair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    version: str, prerelease: bool, actual: bool,
) -> None:
    metadata = release_metadata(version)
    assert metadata == {
        "tag": f"v{version}", "prerelease": str(prerelease).lower(),
        "wheel": f"hermes_local_knowledge-{version}-py3-none-any.whl",
        "sdist": f"hermes_local_knowledge-{version}.tar.gz",
    }
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        f"## [{version}] - 2026-09-19\n\n### Fixed\n\n- Beta test.\n\n"
        f"[{version}]: https://example.test/compare/v0.4.20...v{version}\n"
        "[0.5.2b1]: https://example.test/old\n", encoding="utf-8",
    )
    output = tmp_path / "notes.md"
    args = ["--version", version, "--changelog", str(changelog), "--output", str(output)]
    assert main(args) == 0
    notes = output.read_text(encoding="utf-8")
    assert f"[{version}]:" not in notes and "[0.5.2b1]:" not in notes
    release = tmp_path / "release.json"
    payload = {
        "body": notes, "isDraft": False, "isPrerelease": actual,
        "assets": [{"name": metadata[key], "size": 10, "state": "uploaded"}
                   for key in ("wheel", "sdist")],
    }
    release.write_text(json.dumps(payload), encoding="utf-8")
    args += ["--release-json", str(release), "--expected-wheel", metadata["wheel"],
             "--expected-sdist", metadata["sdist"]]
    assert main([*args, "--inspect-release"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["needed"] is (actual != prerelease)
    assert state["build_needed"] is False
    assert main([*args, "--verify-complete"]) == (0 if actual == prerelease else 1)
    payload["isPrerelease"] = prerelease
    release.write_text(json.dumps(payload), encoding="utf-8")
    assert main([*args, "--verify-complete"]) == 0


def bash_candidates(*, windows: bool) -> list[str]:
    candidates: list[str] = []
    if windows:
        # System32/bash.exe is a WSL launcher, not a local Windows shell.
        git = shutil.which("git")
        roots = [Path(git).parent.parent] if git else []
        for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if value := os.environ.get(key):
                roots.append(Path(value) / ("Programs/Git" if key == "LOCALAPPDATA" else "Git"))
        for directory in roots:
            candidates.extend(str(directory / suffix) for suffix in ("bin/bash.exe", "usr/bin/bash.exe"))
    if bash := shutil.which("bash"):
        candidates.append(bash)
    return list(dict.fromkeys(candidates))


def operational_bash(candidates: list[str], *, windows: bool) -> str:
    failures: list[str] = []
    for candidate in candidates:
        normalized = candidate.replace("\\", "/").lower()
        if windows and any(part in normalized for part in ("/system32/", "/sysnative/", "/windowsapps/")):
            continue
        # Execute Bash syntax and check its platform, not just executable presence.
        probe = '[[ -n "$BASH_VERSION" ]] && printf "%s" "$OSTYPE"'
        try:
            result = subprocess.run(
                [candidate, "--noprofile", "--norc", "-c", probe],
                capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{candidate}: {type(exc).__name__}")
            continue
        if result.returncode == 0 and (not windows or result.stdout.startswith((b"msys", b"cygwin"))):
            return candidate
        failures.append(f"{candidate}: exit={result.returncode}, stdout={result.stdout!r}")
    raise RuntimeError("A working local Bash is required (install Git for Windows on Windows): " + "; ".join(failures))


@pytest.fixture(scope="module")
def bash() -> str:
    return operational_bash(bash_candidates(windows=os.name == "nt"), windows=os.name == "nt")


@pytest.mark.parametrize("failure", ["exit", "missing", "timeout", "wsl"])
def test_bash_probe_rejects_unusable_shell_and_tries_next(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    calls: list[str] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command[0])
        assert kwargs["timeout"] == 10
        if command[0] == "broken":
            if failure == "missing":
                raise FileNotFoundError()
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 10)
            return subprocess.CompletedProcess(command, 1 if failure == "exit" else 0, b"linux-gnu")
        return subprocess.CompletedProcess(command, 0, b"msys")

    monkeypatch.setattr(subprocess, "run", run)
    assert operational_bash(["C:\\Windows\\System32\\bash.exe", "broken", "git-bash"], windows=True) == "git-bash"
    assert calls == ["broken", "git-bash"]
    with pytest.raises(RuntimeError, match="working local Bash"):
        operational_bash(["broken"], windows=True)


def test_windows_bash_candidates_prefer_git_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = tmp_path / "Git" / "cmd" / "git.exe"
    monkeypatch.setattr(shutil, "which", lambda name: str(git) if name == "git" else "C:/Windows/System32/bash.exe")
    candidates = bash_candidates(windows=True)
    assert candidates[:2] == [str(git.parent.parent / suffix) for suffix in ("bin/bash.exe", "usr/bin/bash.exe")]
    assert candidates[-1] == "C:/Windows/System32/bash.exe"


def workflow_run(name: str) -> str:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    step = workflow.split(f"      - name: {name}\n", 1)[1].split("      - name:", 1)[0]
    return "\n".join(line[10:] for line in step.split("        run: |\n", 1)[1].splitlines())


@pytest.mark.parametrize("name", [
    "Repair prerelease status", "Create GitHub release from existing tag",
    "Create tag and GitHub release", "Publish verified draft release",
])
@pytest.mark.parametrize("prerelease", ["true", "false"])
def test_release_workflow_executes_expected_status_flags(
    tmp_path: Path, bash: str, name: str, prerelease: str,
) -> None:
    # Run the actual workflow shell with a recording gh function, never GitHub.
    captured = tmp_path / "args"
    script = 'gh() { printf "%s\n" "$@" > "$CAPTURED"; };\n' + workflow_run(name)
    subprocess.run([bash, "--noprofile", "--norc", "-c", script], check=True, env={
        **os.environ, "CAPTURED": captured.as_posix(), "EXPECTED_PRERELEASE": prerelease,
        "RELEASE_TAG": "v0.5.3b1" if prerelease == "true" else "v0.5.3",
        "GITHUB_REPOSITORY": "fixture/fixture", "RELEASE_NOTES_FILE": "notes.md",
        "HEAD_SHA": "0" * 40,
    })
    args = captured.read_text().splitlines()
    assert f"--prerelease={prerelease}" in args
    assert ("--latest=false" in args) is (prerelease == "true")
    assert "--latest=true" not in args  # Do not promote repaired historical stable releases.

@pytest.mark.parametrize("latest,status,success", [
    ('{"tag_name":"v0.4.20"}', 0, True),
    ('{"tag_name":"v0.5.3b1"}', 0, False),
    ('{"status":"404"}', 1, True),
    ('{"status":"403"}', 1, False),
])
def test_beta_final_verification_rejects_latest_and_api_errors(
    tmp_path: Path, bash: str, latest: str, status: int, success: bool,
) -> None:
    # Exercise the real jq executable too; missing dependencies must not make
    # the rejection cases pass for the wrong reason.
    subprocess.run([bash, "--noprofile", "--norc", "-c", "jq -en true"],
                   check=True, capture_output=True, timeout=10)
    script = r'''gh() {
      if [[ "$1" == api ]]; then printf '%s' "$LATEST"; return "$STATUS"; fi
      printf '{}'
    }
    python() { :; }
    git() { if [[ "$1" == rev-list ]]; then printf '%s' "$EXPECTED_SHA"; fi; }
    ''' + workflow_run("Verify published release")
    result = subprocess.run([bash, "--noprofile", "--norc", "-c", script], capture_output=True, text=True, env={
        **os.environ, "EXPECTED_PRERELEASE": "true", "RELEASE_TAG": "v0.5.3b1",
        "GITHUB_REPOSITORY": "fixture/fixture", "RELEASE_JSON_FILE": (tmp_path / "release.json").as_posix(),
        "RELEASE_NOTES_SCRIPT": "unused", "TAG_CHANGELOG_FILE": "unused",
        "RELEASE_NOTES_FILE": "unused", "EXPECTED_WHEEL": "unused", "EXPECTED_SDIST": "unused",
        "EXPECTED_SHA": "0" * 40, "LATEST": latest, "STATUS": str(status),
    })
    assert (result.returncode == 0) is success, result.stderr
    assert (tmp_path / "release.json").read_text(encoding="utf-8") == "{}"
    if latest == '{"tag_name":"v0.5.3b1"}':
        assert "Beta release must not be latest." in result.stderr
