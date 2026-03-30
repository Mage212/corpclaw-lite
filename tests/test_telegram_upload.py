"""Tests for Telegram upload path sanitization and helpers."""

import pytest

from corpclaw_lite.channels.telegram.upload import (
    is_safe_extension,
    is_image,
    sanitize_filename,
    build_agent_directive,
)


def test_is_safe_extension():
    assert is_safe_extension("doc.pdf") is True
    assert is_safe_extension("doc.exe") is False
    assert is_safe_extension("no_extension") is False
    assert is_safe_extension("hidden.txt") is True


def test_is_image():
    assert is_image("photo.jpg") is True
    assert is_image("photo.PNG") is True
    assert is_image("doc.pdf") is False


def test_sanitize_filename():
    assert sanitize_filename("safe.txt") == "safe.txt"
    assert sanitize_filename("..") is None
    assert sanitize_filename(".") is None
    assert sanitize_filename("\x00trick.txt") is None
    
    # Path traversal
    assert sanitize_filename("../../../etc/passwd") is None
    assert sanitize_filename("C:\\Windows\\System32\\cmd.exe") == "_Windows_System32_cmd.exe"
    assert sanitize_filename("C:\\folder\\file.txt") == "_folder_file.txt"
    
    # Advanced logic matches
    assert sanitize_filename("a/b/c.txt") == "a_b_c.txt"
    assert sanitize_filename(" image.jpg.exe ") is None


def test_build_agent_directive():
    d1 = build_agent_directive("image.jpg", caption=None)
    assert "read_image" in d1
    
    d2 = build_agent_directive("image.jpg", caption="What is this?")
    assert "What is this?" in d2
    assert "read_image" in d2
    
    d3 = build_agent_directive("doc.pdf", caption=None)
    assert "Пользователь загрузил файл" in d3
    
    d4 = build_agent_directive("doc.pdf", caption="Summarize")
    assert "Summarize" in d4
