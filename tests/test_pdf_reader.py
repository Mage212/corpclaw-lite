"""Tests for PdfReaderTool."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.extensions.tools.builtin.pdf_reader import (
    PdfReaderTool,
    _parse_page_range,
    _sanitize_pdf_text,
)


@pytest.fixture
def tool() -> PdfReaderTool:
    return PdfReaderTool()


def _create_simple_pdf(path: Path, pages_text: list[str]) -> Path:
    """Create a minimal PDF with pypdf."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _text in pages_text:
        writer.add_blank_page(width=200, height=200)
        # Add text as metadata (pypdf doesn't easily add visible text).
        # For testing, we rely on the extraction logic.
    writer.write(str(path))
    return path


def _create_pdf_with_text(path: Path, text: str) -> Path:
    """Create a PDF with actual extractable text using reportlab-style approach."""
    # Use a simple approach: create PDF bytes manually.
    # Minimal valid PDF with text.
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(str(path))
    return path


# --- Unit tests ---


class TestParsePageRange:
    def test_all(self) -> None:
        result = _parse_page_range("all", 10)
        assert result == list(range(10))

    def test_range(self) -> None:
        result = _parse_page_range("1-3", 10)
        assert result == [0, 1, 2]

    def test_specific_pages(self) -> None:
        result = _parse_page_range("1,3,5", 10)
        assert result == [0, 2, 4]

    def test_mixed_range(self) -> None:
        result = _parse_page_range("1-2,5,7-8", 10)
        assert result == [0, 1, 4, 6, 7]

    def test_out_of_range(self) -> None:
        result = _parse_page_range("99", 5)
        assert result == []  # Out of range pages are silently skipped.

    def test_clamp_range(self) -> None:
        result = _parse_page_range("8-15", 10)
        assert result == [7, 8, 9]  # Clamped to available pages.


class TestPdfTextSanitizer:
    def test_removes_control_chars_but_preserves_text_symbols(self) -> None:
        raw = "π θ ϵ → —\n\tkeep\x00drop\x01also\x10bad\x11chars\x1a"

        result = _sanitize_pdf_text(raw)

        assert result.removed_control_chars == 5
        assert result.text == "π θ ϵ → —\n\tkeepdropalsobadchars"


# --- Integration tests ---


class TestPdfReaderTool:
    @pytest.mark.asyncio
    async def test_read_blank_pdf(
        self, tool: PdfReaderTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_simple_pdf(tmp_path / "test.pdf", ["", "", ""])

        result = await tool.execute(path="test.pdf")
        assert "Total pages: 3" in result

    @pytest.mark.asyncio
    async def test_page_range(
        self, tool: PdfReaderTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_simple_pdf(tmp_path / "test.pdf", ["", "", "", "", ""])

        result = await tool.execute(path="test.pdf", pages="1-3")
        assert "Extracted: 3" in result

    @pytest.mark.asyncio
    async def test_specific_pages(
        self, tool: PdfReaderTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_simple_pdf(tmp_path / "test.pdf", ["", "", "", "", ""])

        result = await tool.execute(path="test.pdf", pages="1,3,5")
        assert "Extracted: 3" in result

    @pytest.mark.asyncio
    async def test_file_not_found(self, tool: PdfReaderTool) -> None:
        result = await tool.execute(path="nonexistent.pdf")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_not_pdf(
        self, tool: PdfReaderTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "test.txt").write_text("hello", encoding="utf-8")

        result = await tool.execute(path="test.txt")
        assert "Error" in result
        assert "Not a PDF" in result

    @pytest.mark.asyncio
    async def test_max_chars(
        self, tool: PdfReaderTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_simple_pdf(tmp_path / "test.pdf", [""])

        result = await tool.execute(path="test.pdf", max_chars=10)
        # Should not crash even with small max_chars.
        assert "Total pages: 1" in result

    @pytest.mark.asyncio
    async def test_missing_path(self, tool: PdfReaderTool) -> None:
        result = await tool.execute()
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_extracted_text_is_sanitized(
        self, tool: PdfReaderTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pypdf

        class FakePage:
            def extract_text(self) -> str:
                return "Formula r\x00x, yi\x01 and πθ stay"

        class FakeReader:
            is_encrypted = False
            pages = [FakePage()]

            def __init__(self, _path: str) -> None:
                pass

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
        (tmp_path / "test.pdf").write_bytes(b"%PDF fake")

        result = await tool.execute(path="test.pdf")

        assert "\x00" not in result
        assert "\x01" not in result
        assert "Formula rx, yi and πθ stay" in result
        assert "Warning: removed 2 control character(s)" in result

    @pytest.mark.asyncio
    async def test_output_path_writes_sanitized_text(
        self, tool: PdfReaderTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pypdf

        class FakePage:
            def extract_text(self) -> str:
                return "Clean\x00 PDF text"

        class FakeReader:
            is_encrypted = False
            pages = [FakePage()]

            def __init__(self, _path: str) -> None:
                pass

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
        (tmp_path / "test.pdf").write_bytes(b"%PDF fake")

        result = await tool.execute(path="test.pdf", output_path="out/article.md")

        output = (tmp_path / "out" / "article.md").read_text(encoding="utf-8")
        assert "Extracted PDF text from test.pdf to article.md" in result
        assert "Removed control characters: 1" in result
        assert "\x00" not in output
        assert "Clean PDF text" in output

    @pytest.mark.asyncio
    async def test_output_path_rejects_unsafe_extension(
        self, tool: PdfReaderTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "test.pdf").write_bytes(b"%PDF fake")

        result = await tool.execute(path="test.pdf", output_path="out/article.bin")

        assert "Error: output_path must end with one of" in result

    @pytest.mark.asyncio
    async def test_output_path_extracts_full_document_no_truncation(
        self, tool: PdfReaderTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When output_path is given, the ENTIRE document is saved — max_chars
        does not apply. Regression: a "convert PDF to .md" task looped 6
        iterations re-extracting truncated chunks instead of one step."""
        import pypdf

        # Create a PDF that would be truncated by the default _MAX_CHARS (50k).
        # Each page has ~10k chars, 6 pages = 60k > 50k default.
        big_text = "A" * 10_000

        class FakePage:
            def extract_text(self) -> str:
                return big_text

        class FakeReader:
            is_encrypted = False
            pages = [FakePage() for _ in range(6)]

            def __init__(self, _path: str) -> None:
                pass

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
        (tmp_path / "test.pdf").write_bytes(b"%PDF fake")

        result = await tool.execute(path="test.pdf", output_path="out.md")

        assert "truncated" not in result.lower()
        assert "Extracted pages: 6" in result
        output = (tmp_path / "out.md").read_text(encoding="utf-8")
        # Full document — all 6 pages worth of text present.
        assert len(output) > 50_000
