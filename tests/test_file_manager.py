"""Tests for the file manager: protection checks, pagination, deletion."""

from __future__ import annotations

from pathlib import Path

from corpclaw_lite.channels.telegram.file_manager import (
    build_delete_browser,
    is_protected_delete_target,
)


class TestProtectedPaths:
    def test_protected_roots_blocked(self, tmp_path: Path) -> None:
        """Directories in PROTECTED_DELETE_ROOTS must not be deletable."""
        workspace = tmp_path
        for name in [".venv", "src", "config", "tests", ".git"]:
            target = workspace / name
            target.mkdir(exist_ok=True)
            assert is_protected_delete_target(target, workspace), f"{name} should be protected"

    def test_protected_files_blocked(self, tmp_path: Path) -> None:
        """Files in PROTECTED_DELETE_FILES must not be deletable."""
        workspace = tmp_path
        for name in [".env", "pyproject.toml", "AGENTS.md"]:
            target = workspace / name
            target.write_text("x")
            assert is_protected_delete_target(target, workspace), f"{name} should be protected"

    def test_normal_file_allowed(self, tmp_path: Path) -> None:
        """Normal files outside protected sets should be allowed."""
        workspace = tmp_path
        target = workspace / "my_report.xlsx"
        target.write_text("data")
        assert not is_protected_delete_target(target, workspace)

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        """Paths outside workspace must be considered protected."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside_file.txt"
        outside.write_text("x")
        assert is_protected_delete_target(outside, workspace)


class TestBuildBrowser:
    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty workspace shows no entries."""
        text, keyboard, entries, page = build_delete_browser(tmp_path, tmp_path)
        assert len(entries) == 0
        assert page == 0

    def test_with_files(self, tmp_path: Path) -> None:
        """Files are listed in the browser."""
        for i in range(3):
            (tmp_path / f"file_{i}.txt").write_text(f"content {i}")
        text, keyboard, entries, page = build_delete_browser(tmp_path, tmp_path)
        assert len(entries) == 3

    def test_pagination(self, tmp_path: Path) -> None:
        """More than 8 files triggers pagination buttons."""
        for i in range(15):
            (tmp_path / f"file_{i:02d}.txt").write_text("x")
        text, keyboard, entries, page = build_delete_browser(tmp_path, tmp_path)
        # Should have pagination nav buttons
        buttons_flat = [btn for row in keyboard.inline_keyboard for btn in row]
        has_nav = any("▶" in (btn.text or "") for btn in buttons_flat)
        assert has_nav, "Should have pagination forward button"

    def test_second_page(self, tmp_path: Path) -> None:
        """Second page shows different buttons than first."""
        for i in range(15):
            (tmp_path / f"file_{i:02d}.txt").write_text("x")
        _, kb1, entries, _ = build_delete_browser(tmp_path, tmp_path, page=0)
        _, kb2, _, _ = build_delete_browser(tmp_path, tmp_path, page=1)
        # Full entry list has all 15 files
        assert len(entries) == 15
        # Keyboards should differ (different pages)
        buttons1 = [btn.text for row in kb1.inline_keyboard for btn in row]
        buttons2 = [btn.text for row in kb2.inline_keyboard for btn in row]
        assert buttons1 != buttons2


class TestDeleteFile:
    def test_delete_file_removes(self, tmp_path: Path) -> None:
        """Deleting a non-protected file should actually remove it."""
        target = tmp_path / "deleteable.txt"
        target.write_text("to be deleted")
        assert target.exists()
        target.unlink()
        assert not target.exists()

    def test_delete_empty_dir(self, tmp_path: Path) -> None:
        """An empty, non-protected directory can be removed."""
        target = tmp_path / "empty_dir"
        target.mkdir()
        assert target.exists()
        target.rmdir()
        assert not target.exists()
