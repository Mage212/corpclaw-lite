"""Tests for Telegram upload path sanitization and helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from corpclaw_lite.channels.telegram.upload import (
    build_agent_directive,
    is_image,
    is_safe_extension,
    sanitize_filename,
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


def test_xlsx_brief_failure_is_logged_not_swallowed(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    """CR-2: an exception building the FILES_BRIEF must be logged (DC-008),
    not silently swallowed. The directive still returns, just without the brief.
    """
    import openpyxl

    from corpclaw_lite.agent import workbook_brief as wb_mod

    # A real xlsx so the suffix code-path reaches build_workbook_brief.
    xlsx = tmp_path / "report.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Daily"
    ws["A1"] = "Дата"
    wb.save(str(xlsx))
    wb.close()

    def _boom(_paths):
        raise RuntimeError("simulated brief failure")

    # build_workbook_brief is imported lazily inside _xlsx_brief_suffix, so we
    # patch it on its source module (workbook_brief) to intercept the call.
    monkeypatch.setattr(wb_mod, "build_workbook_brief", _boom)
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="corpclaw_lite.channels.telegram.upload"):
        directive = build_agent_directive("report.xlsx", caption=None)

    # Directive returned (fallback path), no crash.
    assert "Пользователь загрузил файл" in directive
    # The exception was logged, not swallowed.
    assert any(
        "xlsx brief failed" in rec.message and "simulated brief failure" in rec.message
        for rec in caplog.records
    ), f"expected warning log, got: {[r.message for r in caplog.records]}"
