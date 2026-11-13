from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from apo_engine.atomic_io import atomic_write_text


def test_atomic_write_replaces_content_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)

    atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_new_file_respects_umask(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    previous = os.umask(0o027)
    try:
        atomic_write_text(target, "new")
    finally:
        os.umask(previous)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_atomic_write_cleans_temp_after_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "note.md"

    def fail_replace(_src: Path, _dst: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "new")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX durability")
def test_atomic_write_fsyncs_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "note.md"
    real_fsync = os.fsync
    real_replace = os.replace
    synced_modes: list[int] = []
    events: list[str] = []

    def record_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        synced_modes.append(mode)
        events.append("directory_fsync" if stat.S_ISDIR(mode) else "file_fsync")
        real_fsync(fd)

    def record_replace(src: Path, dst: Path) -> None:
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)
    atomic_write_text(target, "new")

    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)
    assert events.index("replace") < events.index("directory_fsync")
