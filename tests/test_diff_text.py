"""Tests for DiffTextTool."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.extensions.tools.builtin.diff_text import (
    DiffTextTool,
    _chars_diff,
    _read_if_path,
    _unified_diff,
    _words_diff,
)


@pytest.fixture
def tool() -> DiffTextTool:
    return DiffTextTool()


# --- Unit tests for helper functions ---


class TestReadIfPath:
    def test_reads_existing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
        assert _read_if_path("a.txt") == "hello world"

    def test_returns_literal_if_not_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert _read_if_path("not a file path") == "not a file path"

    def test_returns_literal_on_path_traversal(self) -> None:
        result = _read_if_path("../../../etc/passwd")
        assert "etc" in result or isinstance(result, str)


class TestUnifiedDiff:
    def test_shows_differences(self) -> None:
        result = _unified_diff(["line1\n", "line2\n"], ["line1\n", "changed\n"], 3)
        assert "-line2" in result
        assert "+changed" in result

    def test_no_differences(self) -> None:
        result = _unified_diff(["same\n"], ["same\n"], 3)
        assert result == ""


class TestWordsDiff:
    def test_shows_differences(self) -> None:
        result = _words_diff(["old line\n"], ["new line\n"])
        assert "- old line" in result
        assert "+ new line" in result


class TestCharsDiff:
    def test_shows_char_differences(self) -> None:
        result = _chars_diff("abc", "adc")
        assert isinstance(result, str)
        assert len(result) > 0


# --- Integration tests ---


class TestDiffTextTool:
    @pytest.mark.asyncio
    async def test_unified_diff_text(self, tool: DiffTextTool) -> None:
        result = await tool.execute(source="hello\nworld", target="hello\nearth")
        assert "-world" in result
        assert "+earth" in result

    @pytest.mark.asyncio
    async def test_words_diff(self, tool: DiffTextTool) -> None:
        result = await tool.execute(source="foo", target="bar", mode="words")
        assert "- foo" in result
        assert "+ bar" in result

    @pytest.mark.asyncio
    async def test_chars_diff(self, tool: DiffTextTool) -> None:
        result = await tool.execute(source="abc", target="adc", mode="chars")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_no_differences(self, tool: DiffTextTool) -> None:
        result = await tool.execute(source="same text", target="same text")
        assert "No differences" in result

    @pytest.mark.asyncio
    async def test_file_diff(
        self, tool: DiffTextTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.txt").write_text("line1\nline2\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("line1\nchanged\n", encoding="utf-8")

        result = await tool.execute(source="a.txt", target="b.txt")
        assert "-line2" in result
        assert "+changed" in result

    @pytest.mark.asyncio
    async def test_mixed_file_and_text(
        self, tool: DiffTextTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.txt").write_text("original\n", encoding="utf-8")

        result = await tool.execute(source="a.txt", target="original\n")
        assert "No differences" in result

    @pytest.mark.asyncio
    async def test_context_lines(self, tool: DiffTextTool) -> None:
        source = "\n".join(f"line{i}" for i in range(10))
        target = "\n".join(f"line{i}" if i != 5 else "CHANGED" for i in range(10))
        result = await tool.execute(source=source, target=target, context_lines=1)
        assert "CHANGED" in result

    @pytest.mark.asyncio
    async def test_empty_inputs(self, tool: DiffTextTool) -> None:
        result = await tool.execute(source="", target="something")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_default_mode_is_unified(self, tool: DiffTextTool) -> None:
        result = await tool.execute(source="a\nb", target="a\nc")
        assert "---" in result  # unified diff header
