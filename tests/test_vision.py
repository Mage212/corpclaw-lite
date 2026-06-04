"""Tests for VisionProcessor real implementation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.agent.vision import VisionProcessor
from corpclaw_lite.llm.base import LLMResponse


class FakeVisionProvider:
    """Provider that supports chat_with_image."""

    def __init__(self, response_text: str = "A cute cat.") -> None:
        self._response_text = response_text
        self.last_image_data: str | None = None
        self.last_media_type: str | None = None
        self.last_prompt: str | None = None

    async def chat(self, **kwargs: object) -> LLMResponse:
        return LLMResponse(content="fallback text")

    async def chat_with_image(
        self,
        image_data: str,
        image_media_type: str,
        prompt: str,
        system: str | None = None,
    ) -> LLMResponse:
        self.last_image_data = image_data
        self.last_media_type = image_media_type
        self.last_prompt = prompt
        return LLMResponse(content=self._response_text)


class FakeTextOnlyProvider:
    """Provider without chat_with_image — triggers fallback."""

    async def chat(self, **kwargs: object) -> LLMResponse:
        return LLMResponse(content="text-only fallback")


@pytest.mark.asyncio
async def test_vision_base64_encoding(tmp_path: Path) -> None:
    """Verify base64 data is passed to provider."""
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    provider = FakeVisionProvider("A test image")
    vp = VisionProcessor(provider)

    result = await vp.describe(img, "Describe this image")

    assert result == "A test image"
    assert provider.last_image_data is not None
    assert provider.last_media_type == "image/png"
    assert provider.last_prompt == "Describe this image"

    # Verify it's valid base64
    import base64

    decoded = base64.b64decode(provider.last_image_data)
    assert decoded.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_vision_fallback_no_vision(tmp_path: Path) -> None:
    """Provider without chat_with_image uses text-only fallback."""
    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)

    provider = FakeTextOnlyProvider()
    vp = VisionProcessor(provider)

    result = await vp.describe(img, "What is this?")
    assert result == "text-only fallback"


@pytest.mark.asyncio
async def test_vision_file_not_found() -> None:
    provider = FakeVisionProvider()
    vp = VisionProcessor(provider)

    result = await vp.describe(Path("/nonexistent/image.png"), "Describe")
    assert "Error" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_vision_unsupported_format(tmp_path: Path) -> None:
    f = tmp_path / "data.tiff"
    f.write_bytes(b"\x00" * 10)

    provider = FakeVisionProvider()
    vp = VisionProcessor(provider)

    result = await vp.describe(f, "Describe")
    assert "Error" in result
    assert "Unsupported" in result


@pytest.mark.asyncio
async def test_vision_rejects_oversized_image_before_provider_call(tmp_path: Path) -> None:
    img = tmp_path / "large.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    provider = FakeVisionProvider()
    vp = VisionProcessor(provider, max_image_bytes=8)

    result = await vp.describe(img, "Describe")

    assert "Error" in result
    assert "too large" in result
    assert provider.last_image_data is None


@pytest.mark.asyncio
async def test_vision_provider_error(tmp_path: Path) -> None:
    """Provider raises an exception — should be caught."""
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 50)

    provider = FakeVisionProvider()
    provider.chat_with_image = AsyncMock(side_effect=RuntimeError("API down"))  # type: ignore[method-assign]
    vp = VisionProcessor(provider)

    result = await vp.describe(img, "Describe")
    assert "Error" in result
    assert "API down" in result
