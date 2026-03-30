"""Tests for ReadImageTool.execute() — covers all branches of image.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.agent.vision import VisionProcessor
from corpclaw_lite.extensions.tools.builtin.image import ReadImageTool
from corpclaw_lite.llm.base import LLMResponse


class FakeVisionProvider:
    """Minimal provider with chat_with_image support."""

    async def chat(self, **kwargs: object) -> LLMResponse:
        return LLMResponse(content="fallback")

    async def chat_with_image(
        self,
        image_data: str,
        image_media_type: str,
        prompt: str,
        system: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="I see a cat in this image.")


@pytest.fixture
def tool() -> ReadImageTool:
    provider = FakeVisionProvider()
    processor = VisionProcessor(provider)
    return ReadImageTool(processor)


@pytest.fixture(autouse=True)
def _chdir_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set CWD to tmp_path so resolve_and_validate_path allows access."""
    monkeypatch.chdir(tmp_path)


@pytest.mark.asyncio
async def test_read_image_missing_path(tool: ReadImageTool) -> None:
    """execute() with no path returns error."""
    result = await tool.execute()
    assert "Error" in result
    assert "missing" in result


@pytest.mark.asyncio
async def test_read_image_nonexistent_file(tool: ReadImageTool, tmp_path: Path) -> None:
    """execute() with nonexistent file returns error."""
    result = await tool.execute(path=str(tmp_path / "ghost.png"))
    assert "does not exist" in result


@pytest.mark.asyncio
async def test_read_image_not_image_extension(tool: ReadImageTool, tmp_path: Path) -> None:
    """execute() with a .txt file rejects it."""
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    result = await tool.execute(path=str(txt), prompt="Describe")
    assert "only accepts image files" in result


@pytest.mark.asyncio
async def test_read_image_success(tool: ReadImageTool, tmp_path: Path) -> None:
    """execute() with a real .png file delegates to VisionProcessor and returns text."""
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    result = await tool.execute(path=str(img), prompt="What is this?")
    assert "cat" in result


@pytest.mark.asyncio
async def test_read_image_default_prompt(tool: ReadImageTool, tmp_path: Path) -> None:
    """execute() without prompt uses the default prompt."""
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)
    result = await tool.execute(path=str(img))
    assert "cat" in result


@pytest.mark.asyncio
async def test_read_image_processor_error(tmp_path: Path) -> None:
    """execute() catches exceptions from the VisionProcessor."""
    provider = FakeVisionProvider()
    processor = VisionProcessor(provider)
    processor.describe = AsyncMock(side_effect=RuntimeError("API down"))  # type: ignore[method-assign]
    tool = ReadImageTool(processor)

    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    result = await tool.execute(path=str(img), prompt="Describe")
    assert "Error" in result
    assert "API down" in result
