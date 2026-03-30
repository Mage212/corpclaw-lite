"""Tests for Telegram file manager utility functions."""

import os
from pathlib import Path

from corpclaw_lite.channels.telegram.file_manager import (
    _format_size,
    _is_within_workspace,
    _relative_display,
    is_protected_delete_target,
    _list_entries,
    build_delete_browser,
    build_delete_confirmation,
)

def test_format_size():
    assert _format_size(500) == "500 B"
    assert _format_size(1024) == "1.0 KB"
    assert _format_size(1536) == "1.5 KB"
    assert _format_size(1048576) == "1.0 MB"
    assert _format_size(25165824) == "24.0 MB"

def test_is_within_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    
    inside = ws / "test.txt"
    outside = tmp_path / "other.txt"
    up_dir = ws / ".." / "other.txt"
    
    assert _is_within_workspace(inside, ws) is True
    assert _is_within_workspace(outside, ws) is False
    assert _is_within_workspace(up_dir, ws) is False
    assert _is_within_workspace(ws, ws) is True

def test_relative_display(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    
    f1 = ws / "folder" / "file.txt"
    assert _relative_display(f1, ws) == "folder/file.txt"
    
    assert _relative_display(ws, ws) == "."
    
    outside = tmp_path / "other.txt"
    # Fallback to absolute if outside
    assert _relative_display(outside, ws) == str(outside.resolve())

def test_is_protected_delete_target(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    
    # Normal file
    f1 = ws / "file.txt"
    assert is_protected_delete_target(f1, ws) is False
    
    # Protected roots
    for root in [".git", "config", "tests"]:
        protected_dir = ws / root
        assert is_protected_delete_target(protected_dir, ws) is True
        nested = protected_dir / "something.txt"
        assert is_protected_delete_target(nested, ws) is True
        
    # Protected files at root
    for f in [".env", "README.md", "pyproject.toml"]:
        protected_file = ws / f
        assert is_protected_delete_target(protected_file, ws) is True
        
    # Same file name nested is NOT protected
    nested_readme = ws / "folder" / "README.md"
    assert is_protected_delete_target(nested_readme, ws) is False
    
    # Outside workspace is protected (prevents deleting /etc/passwd etc)
    outside = tmp_path / "outside.txt"
    assert is_protected_delete_target(outside, ws) is True

def test_list_entries_and_build(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    
    (ws / "a.txt").write_text("hello")
    (ws / "b_dir").mkdir()
    (ws / ".git").mkdir()  # Protected
    (ws / ".env").write_text("secret") # Protected
    
    entries, hidden = _list_entries(ws, ws)
    
    # .git and .env are hidden
    assert hidden == 2
    
    # b_dir and a.txt should be visible. Directories come first.
    assert len(entries) == 2
    assert entries[0].name == "b_dir"
    assert entries[0].is_dir is True
    assert entries[1].name == "a.txt"
    assert entries[1].is_dir is False
    assert entries[1].size_bytes == 5
    
    # build_delete_browser
    text, kb, returned_entries, page = build_delete_browser(ws, ws)
    assert len(returned_entries) == 2
    assert "a\\.txt" in text
    assert "b\\_dir" in text
    assert "скрытые служебные: 2" in text
    
    # build_delete_confirmation
    text, kb = build_delete_confirmation(ws / "a.txt", ws)
    assert "Подтверждение удаления" in text
    assert "a\\.txt" in text

def test_build_delete_browser_pagination(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    
    # Create 15 files
    for i in range(15):
        (ws / f"file_{i:02d}.txt").touch()
        
    # Page 0 should have 8 items (DELETE_PAGE_SIZE)
    text0, kb0, entries0, page0 = build_delete_browser(ws, ws, page=0)
    assert page0 == 0
    assert len(entries0) == 15
    # The actual inline keyboard will have 8 files + nav + actions
    
    # Page 1 should have the remaining 7 items
    text1, kb1, entries1, page1 = build_delete_browser(ws, ws, page=1)
    assert page1 == 1

