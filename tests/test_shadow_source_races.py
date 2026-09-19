"""Deterministic source-tree races at the provider evidence boundary."""
from __future__ import annotations

import errno
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hermes_local_knowledge import index, shadow_sources
from hermes_local_knowledge.config import Config, IndexSettings


@pytest.fixture
def source(tmp_path: Path) -> tuple[Config, str, Path]:
    root, home, state = (tmp_path / name for name in ("corpus", "profile", "state"))
    path = root / "docs" / "guide.md"
    path.parent.mkdir(parents=True)
    home.mkdir()
    path.write_bytes(b"# Approved evidence\n")
    cfg = Config(source_root=root, hermes_home=home, state_dir=state,
                 index_settings=IndexSettings())
    index.build_index(root, state, home, cfg.index_settings)
    return cfg, "runbook:docs-guide", path


@pytest.fixture
def symlinks(tmp_path: Path) -> None:
    """Only link-specific cases require Windows symlink privileges."""
    if os.name == "nt":
        link = tmp_path / "symlink-probe"
        try:
            link.symlink_to(tmp_path, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege unavailable")
            raise
        link.unlink()


@pytest.mark.usefixtures("symlinks")
@pytest.mark.parametrize("target", ["file", "ancestor", "root"])
def test_swap_after_validation_never_enters_evidence(
    source: tuple[Config, str, Path], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    cfg, artifact_id, path = source
    outside = tmp_path / "outside"
    (outside / "docs").mkdir(parents=True)
    secret = "SYNTHETIC OUTSIDE ROOT BYTES"
    (outside / "guide.md").write_text(secret, encoding="utf-8")
    (outside / "docs" / "guide.md").write_text(secret, encoding="utf-8")
    original = shadow_sources._source_path
    raced = False

    def swap(config: Config, row: dict[str, Any]) -> Path:
        nonlocal raced
        resolved = original(config, row)
        victim = {"file": path, "ancestor": path.parent, "root": cfg.source_root}[target]
        victim.rename(victim.with_name(victim.name + "-saved"))
        victim.symlink_to(outside / "guide.md" if target == "file" else outside,
                          target_is_directory=target != "file")
        raced = True
        return resolved

    monkeypatch.setattr(shadow_sources, "_source_path", swap)
    evidence = shadow_sources.Evidence(cfg, "")
    evidence.include([artifact_id])
    assert not evidence.try_read(artifact_id)
    assert raced
    assert evidence.packet()["sources"] == []
    assert secret not in str(evidence.packet())


@pytest.mark.parametrize("replacement", ["directory", "fifo"])
def test_nonregular_swap_rejected_before_read(
    source: tuple[Config, str, Path], monkeypatch: pytest.MonkeyPatch, replacement: str,
) -> None:
    if replacement == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("POSIX FIFO primitive")
    cfg, artifact_id, path = source
    original = shadow_sources._source_path

    def swap(config: Config, row: dict[str, Any]) -> Path:
        resolved = original(config, row)
        path.unlink()
        if replacement == "directory":
            path.mkdir()
        else:
            os.mkfifo(path)
        return resolved

    monkeypatch.setattr(shadow_sources, "_source_path", swap)
    with pytest.raises(ValueError, match="unregistered_source_path"):
        shadow_sources.read_source(cfg, artifact_id)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative open")
@pytest.mark.parametrize("opened", ["directory", "file"])
def test_swap_after_open_keeps_read_bound_to_descriptor(
    source: tuple[Config, str, Path], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, opened: str,
) -> None:
    cfg, artifact_id, path = source
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "guide.md").write_text("SYNTHETIC OUTSIDE ROOT BYTES", encoding="utf-8")
    original_open = os.open
    raced = False

    def racing_open(name: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal raced
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        if name == ("docs" if opened == "directory" else "guide.md") and not raced:
            victim = path.parent if opened == "directory" else path
            victim.rename(victim.with_name(victim.name + "-saved"))
            victim.symlink_to(outside if opened == "directory" else outside / "guide.md",
                              target_is_directory=opened == "directory")
            raced = True
        return fd

    monkeypatch.setattr(os, "open", racing_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {racing_open})
    result = shadow_sources.read_source(cfg, artifact_id)
    assert raced
    assert result["text"] == "# Approved evidence\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow primitives")
def test_missing_secure_primitives_fail_closed(
    source: tuple[Config, str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, artifact_id, _ = source
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(OSError) as error:
        shadow_sources.read_source(cfg, artifact_id)
    assert error.value.errno == errno.ENOTSUP


@pytest.mark.usefixtures("symlinks")
def test_configured_symlink_root_is_still_accepted(
    source: tuple[Config, str, Path], tmp_path: Path,
) -> None:
    cfg, artifact_id, _ = source
    alias = tmp_path / "root-alias"
    alias.symlink_to(cfg.source_root, target_is_directory=True)
    assert shadow_sources.read_source(replace(cfg, source_root=alias), artifact_id)["text"] == (
        "# Approved evidence\n")


def test_runtime_skill_root_outside_corpus_is_still_accepted(
    source: tuple[Config, str, Path],
) -> None:
    cfg, _, _ = source
    skill = cfg.hermes_home / "skills" / "example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: example\ndescription: Example operations\n---\n# Example\n",
                     encoding="utf-8")
    index.build_index(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)
    rows = index.search_index(cfg.state_dir / "index.sqlite", "example", artifact_type="skill")
    assert len(rows) == 1
    assert shadow_sources.read_source(cfg, rows[0]["id"])["locator"] == str(skill.resolve())


@pytest.mark.usefixtures("symlinks")
def test_existing_in_root_symlinks_and_exact_size_are_accepted(
    source: tuple[Config, str, Path],
) -> None:
    cfg, artifact_id, path = source
    target = path.with_name("target.md")
    path.rename(target)
    path.symlink_to(target)
    target.write_bytes(b"a" * shadow_sources.MAX_SOURCE_BYTES)
    result = shadow_sources.read_source(cfg, artifact_id)
    assert result["locator"] == str(target.resolve())
    assert result["bytes"] == shadow_sources.MAX_SOURCE_BYTES
    target.write_bytes(b"a" * (shadow_sources.MAX_SOURCE_BYTES + 1))
    with pytest.raises(ValueError, match="source_too_large"):
        shadow_sources.read_source(cfg, artifact_id)


@pytest.mark.parametrize("size", [0, 17, shadow_sources.MAX_SOURCE_BYTES,
                                  shadow_sources.MAX_SOURCE_BYTES + 1])
def test_regular_source_size_contract_without_symlink_privileges(
    source: tuple[Config, str, Path], size: int,
) -> None:
    cfg, artifact_id, path = source
    path.write_bytes(b"a" * size)
    if size > shadow_sources.MAX_SOURCE_BYTES:
        with pytest.raises(ValueError, match="source_too_large"):
            shadow_sources.read_source(cfg, artifact_id)
    elif size == 0:
        with pytest.raises(ValueError, match="empty_source"):
            shadow_sources.read_source(cfg, artifact_id)
    else:
        result = shadow_sources.read_source(cfg, artifact_id)
        assert result["text"] == "a" * size
        assert result["bytes"] == size


def test_windows_dispatch_does_not_require_posix_primitives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    path = Path("unused.md")
    reader = Mock(return_value=b"approved")
    monkeypatch.setattr(shadow_sources, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(shadow_sources, "_read_source_bytes_windows", reader)
    assert shadow_sources._read_source_bytes(path) == b"approved"
    reader.assert_called_once_with(path)


def test_source_unicode_hash_and_invalid_utf8(source: tuple[Config, str, Path]) -> None:
    import hashlib

    cfg, artifact_id, path = source
    raw = "# Résumé\n来源\n".encode("utf-8")
    path.write_bytes(raw)
    result = shadow_sources.read_source(cfg, artifact_id)
    assert result["text"] == raw.decode("utf-8")
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["bytes"] == len(raw)
    assert result["locator"] == str(path.resolve())
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        shadow_sources.read_source(cfg, artifact_id)


@pytest.mark.parametrize("failure", [None, "open", "info", "read", "ancestor_reparse",
                                     "leaf_reparse", "directory", "device"])
@pytest.mark.parametrize("unc", [False, True])
def test_windows_handle_protocol_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, failure: str | None, unc: bool,
) -> None:
    """Portable API-contract unit test, NOT a native Windows security proof."""
    import ctypes
    from pathlib import PureWindowsPath
    from types import SimpleNamespace
    from unittest.mock import Mock

    path = PureWindowsPath(r"\\server\share\docs\guide.md" if unc else r"C:\docs\guide.md")
    parts = [*reversed(path.parents), path]
    opened: list[int] = []
    closed: list[int] = []
    payload = b"approved"

    def create(name: str, access: int, share: int, security: Any,
               disposition: int, flags: int, template: Any) -> int:
        assert not closed  # Every ancestor stays pinned through the read.
        part = parts[len(opened)]
        expected = ("\\\\?\\UNC\\" + str(part)[2:] if unc else "\\\\?\\" + str(part))
        assert name == expected
        assert share == 1  # No WRITE or DELETE sharing, even on ancestors.
        assert access == 0x80000000  # Attribute-only opens do not pin directories.
        assert disposition == 3
        assert flags == 0x02200000
        assert security is template is None
        if failure == "open" and part == path:
            return ctypes.c_void_p(-1).value  # type: ignore[return-value]
        opened.append(len(opened) + 1)
        return opened[-1]

    def info(handle: int, info_class: int, output: Any, size: int) -> bool:
        assert info_class == 9
        leaf = handle == len(parts)
        output._obj.attributes = 0 if leaf else 0x10
        if failure == "ancestor_reparse" and handle == 2 or failure == "leaf_reparse" and leaf:
            output._obj.attributes |= 0x400
        if failure == "directory" and leaf:
            output._obj.attributes |= 0x10
        return not (failure == "info" and leaf)

    def read(handle: int, buffer: Any, size: int, count: Any, overlapped: Any) -> bool:
        assert handle == opened[-1] == len(parts)
        assert not closed
        assert size == shadow_sources.MAX_SOURCE_BYTES + 1
        assert overlapped is None
        buffer[:len(payload)] = payload
        count._obj.value = len(payload)
        return failure != "read"

    api = SimpleNamespace(
        CreateFileW=Mock(side_effect=create), GetFileInformationByHandleEx=Mock(side_effect=info),
        GetFileType=Mock(return_value=2 if failure == "device" else 1),
        ReadFile=Mock(side_effect=read), CloseHandle=Mock(side_effect=closed.append),
    )
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "synthetic API error"),
                        raising=False)
    if failure:
        with pytest.raises(OSError if failure in {"open", "info", "read"} else ValueError):
            shadow_sources._read_source_bytes_windows(path)  # type: ignore[arg-type]
        if failure != "read":
            api.ReadFile.assert_not_called()
    else:
        assert shadow_sources._read_source_bytes_windows(path) == payload  # type: ignore[arg-type]
    assert closed == list(reversed(opened))
    from ctypes import wintypes
    assert api.CreateFileW.restype is wintypes.HANDLE


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction semantics")
@pytest.mark.parametrize("target", ["ancestor", "root"])
def test_windows_junction_swap_never_enters_evidence(
    source: tuple[Config, str, Path], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    import subprocess

    cfg, artifact_id, path = source
    outside = tmp_path / "outside"
    (outside / "docs").mkdir(parents=True)
    secret = "SYNTHETIC OUTSIDE ROOT BYTES"
    (outside / "guide.md").write_text(secret, encoding="utf-8")
    (outside / "docs" / "guide.md").write_text(secret, encoding="utf-8")
    original = shadow_sources._source_path
    victim = path.parent if target == "ancestor" else cfg.source_root

    def swap(config: Config, row: dict[str, Any]) -> Path:
        resolved = original(config, row)
        victim.rename(victim.with_name(victim.name + "-saved"))
        subprocess.run(["cmd", "/c", "mklink", "/J", str(victim), str(outside)],
                       check=True, capture_output=True)
        return resolved

    monkeypatch.setattr(shadow_sources, "_source_path", swap)
    try:
        evidence = shadow_sources.Evidence(cfg, "")
        evidence.include([artifact_id])
        assert not evidence.try_read(artifact_id)
        assert evidence.packet()["sources"] == []
        assert secret not in str(evidence.packet())
    finally:
        # Remove the junction itself, never recursively traverse its target.
        if victim.exists():
            victim.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="native Windows handle sharing semantics")
@pytest.mark.parametrize("target", ["file", "ancestor", "root"])
def test_windows_open_handles_block_replacement_and_release_after_read(
    source: tuple[Config, str, Path], monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    import ctypes
    from unittest.mock import Mock

    cfg, artifact_id, path = source
    victim = {"file": path, "ancestor": path.parent, "root": cfg.source_root}[target]
    saved = victim.with_name(victim.name + "-saved")
    dll = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    original = dll.ReadFile
    attempted = False

    def racing_read(*args: Any) -> Any:
        nonlocal attempted
        attempted = True
        with pytest.raises(OSError) as error:
            victim.rename(saved)
        assert getattr(error.value, "winerror", None) in {5, 32}
        original.argtypes, original.restype = wrapped.argtypes, wrapped.restype
        return original(*args)

    wrapped = Mock(side_effect=racing_read)
    dll.ReadFile = wrapped
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=dll))
    assert shadow_sources.read_source(cfg, artifact_id)["text"] == "# Approved evidence\n"
    assert attempted
    victim.rename(saved)  # No handles leaked after the bounded read.
    saved.rename(victim)
