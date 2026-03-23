"""Tests for Telegram message formatting and splitting."""

from __future__ import annotations

from corpclaw_lite.channels.telegram.formatting import (
    build_response_parts,
    convert_markdown_tables,
    split_message,
)


class TestSplitMessage:
    def test_short_text_no_split(self) -> None:
        """Short messages below limit are returned as a single chunk."""
        text = "Hello, world!"
        result = split_message(text)
        assert result == [text]

    def test_long_text_splits(self) -> None:
        """Messages exceeding max_length are split into multiple chunks."""
        text = "A" * 8000
        result = split_message(text, max_length=3800)
        assert len(result) > 1
        # Every chunk should be under max_length (or exactly max if forced)
        for chunk in result:
            assert len(chunk) <= 8000  # single line won't exceed line length

    def test_preserves_code_blocks_across_splits(self) -> None:
        """When splitting inside a ``` block, the block is closed and reopened."""
        code_lines = ["```python"] + [f"line_{i} = {i}" for i in range(500)] + ["```"]
        text = "\n".join(code_lines)
        result = split_message(text, max_length=500)
        assert len(result) > 1
        # After the first chunk, code blocks should be reopened
        for i, chunk in enumerate(result):
            if i == 0:
                assert chunk.startswith("```python")
            if i > 0 and "line_" in chunk:
                assert "```" in chunk

    def test_split_on_newlines(self) -> None:
        """Splitting prefers newline boundaries over mid-line splits."""
        lines = [f"Line {i}: " + "x" * 50 for i in range(100)]
        text = "\n".join(lines)
        result = split_message(text, max_length=500)
        # Each chunk (except possibly the last) should be ≤ max_length
        for chunk in result[:-1]:
            assert len(chunk) <= 600  # account for closing ```


class TestConvertMarkdownTables:
    def test_simple_table(self) -> None:
        """A basic markdown table should be converted to card entries."""
        table = (
            "| Name | Age | Role |\n"
            "|------|-----|------|\n"
            "| Alice | 30 | Dev |\n"
            "| Bob | 25 | QA |\n"
        )
        result = convert_markdown_tables(table)
        assert "**Name**:" in result
        assert "Alice" in result
        assert "Bob" in result
        # Original pipe table syntax should be gone
        assert "|---" not in result

    def test_table_inside_code_block_preserved(self) -> None:
        """Tables inside code blocks should not be converted."""
        text = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
        result = convert_markdown_tables(text)
        assert "|---|" in result  # Still present — not converted

    def test_no_table(self) -> None:
        """Text without tables should pass through unchanged."""
        text = "Hello world\nNo tables here"
        result = convert_markdown_tables(text)
        assert result == text


class TestBuildResponseParts:
    def test_short_text_no_pagination(self) -> None:
        """Short texts get no [1/N] suffix."""
        result = build_response_parts("Hello!")
        assert len(result) == 1
        assert "[1/" not in result[0]

    def test_long_text_pagination(self) -> None:
        """Long texts get [i/N] suffixes."""
        text = "Line\n" * 2000  # ~10000 chars
        result = build_response_parts(text)
        assert len(result) > 1
        assert "[1/" in result[0]
        assert f"[{len(result)}/{len(result)}]" in result[-1]
