"""Tests for B-059: atomic write primitives (utils/fs.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from corpclaw_lite.utils.fs import atomic_save_via, atomic_write_text

# ─── atomic_write_text ───────────────────────────────────────────────────────


def test_atomic_write_text_creates_file_with_content(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"


def test_atomic_write_text_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_custom_encoding(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "привет", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "привет"


def test_atomic_write_text_no_temp_file_left_after_success(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "data")
    temp_files = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert temp_files == []


def test_atomic_write_text_cleans_temp_on_exception(tmp_path: Path) -> None:
    """If the write fails (missing parent dir), the temp file is removed."""
    # Write into a path whose parent directory doesn't exist and isn't
    # created by atomic_write_text → os.open fails with FileNotFoundError.
    deep = tmp_path / "missing" / "deep" / "out.txt"
    with pytest.raises(FileNotFoundError):
        atomic_write_text(deep, "data")
    # No temp files left behind anywhere under tmp_path.
    temp_files = [p for p in tmp_path.rglob("*") if ".tmp." in p.name]
    assert temp_files == []


# ─── atomic_save_via ─────────────────────────────────────────────────────────


def test_atomic_save_via_with_callable(tmp_path: Path) -> None:
    """A simple saver callable that writes bytes to the temp path."""
    target = tmp_path / "blob.bin"

    def saver(path: Path) -> None:
        path.write_bytes(b"\x00\x01\x02\x03")

    atomic_save_via(saver, target)
    assert target.read_bytes() == b"\x00\x01\x02\x03"


def test_atomic_save_via_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"old")

    def saver(path: Path) -> None:
        path.write_bytes(b"new")

    atomic_save_via(saver, target)
    assert target.read_bytes() == b"new"


def test_atomic_save_via_cleans_temp_on_exception(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"

    def failing_saver(path: Path) -> None:
        path.write_bytes(b"partial")
        raise RuntimeError("saver exploded")

    with pytest.raises(RuntimeError, match="exploded"):
        atomic_save_via(failing_saver, target)
    # target was never created
    assert not target.exists()
    temp_files = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert temp_files == []


def test_atomic_save_via_with_openpyxl_workbook(tmp_path: Path) -> None:
    """Integration with the openpyxl Workbook.save path used by office tools."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws["A1"] = "hello"

    target = tmp_path / "report.xlsx"
    atomic_save_via(wb.save, target)
    assert target.exists()
    assert target.stat().st_size > 0

    # Verify it's a valid xlsx by re-opening.
    wb2 = openpyxl.load_workbook(target)
    ws2 = wb2.active
    assert ws2 is not None
    assert ws2["A1"].value == "hello"


# ─── O_NOFOLLOW protection ───────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevated perms on Windows")
def test_atomic_write_text_rejects_symlink_target(tmp_path: Path) -> None:
    """If the target path is a symlink, atomic_write_text must fail rather than
    follow it — this is the TOCTOU symlink-swap protection."""
    real = tmp_path / "real.txt"
    real.write_text("original")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    # atomic_write_text opens with O_NOFOLLOW → ELOOP on symlink.
    with pytest.raises(OSError):
        atomic_write_text(link, "hijacked")

    # The real file is untouched.
    assert real.read_text() == "original"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevated perms on Windows")
def test_atomic_write_text_to_regular_file_after_symlink_test(tmp_path: Path) -> None:
    """Sanity: writing to a regular file still works after the symlink test."""
    target = tmp_path / "regular.txt"
    atomic_write_text(target, "content")
    assert target.read_text() == "content"


# ─── same-directory temp invariant ───────────────────────────────────────────


def test_temp_in_same_directory_as_target(tmp_path: Path) -> None:
    """The temp file must live in the same directory (os.replace EXDEV guard)."""
    from corpclaw_lite.utils.fs import _temp_path_for

    target = tmp_path / "deep" / "out.txt"
    target.parent.mkdir()
    temp = _temp_path_for(target)
    assert temp.parent == target.parent
    assert temp.name.startswith("out.txt.tmp.")
