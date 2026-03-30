from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

__all__ = [
    "VisionProcessor",
]

if TYPE_CHECKING:
    from corpclaw_lite.llm.base import Provider
    from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)

_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class VisionProcessor:
    """Handles image reading via a dedicated LLM call with a vision model."""

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    async def describe(self, path: Path, prompt: str, user: User | None = None) -> str:
        """Read an image and get a description/analysis from the vision model."""
        aio_path = anyio.Path(path)
        if not await aio_path.exists() or not await aio_path.is_file():
            return f"Error: Image file not found: {path}"

        media_type = _MEDIA_TYPES.get(path.suffix.lower())
        if not media_type:
            return f"Error: Unsupported image format '{path.suffix}'."

        # Encode image as base64
        try:
            raw = await aio_path.read_bytes()
            image_data = base64.b64encode(raw).decode("ascii")
        except Exception as e:
            return f"Error reading image file: {e}"

        logger.info("VisionProcessor describing %s (%s, %d bytes)", path.name, media_type, len(raw))

        # Resolve the actual provider: if we have a router, use the vision task route
        from corpclaw_lite.llm.base import LLMResponse, VisionProvider
        from corpclaw_lite.llm.router import LLMRouter

        effective_provider: Provider
        if isinstance(self._provider, LLMRouter):
            effective_provider = self._provider.for_task("vision")
        else:
            effective_provider = self._provider

        # Try vision-aware provider first
        if isinstance(effective_provider, VisionProvider):
            try:
                result: LLMResponse = await effective_provider.chat_with_image(
                    image_data=image_data,
                    image_media_type=media_type,
                    prompt=prompt,
                )
                if result.content:
                    return result.content
                return "Vision model returned no description."
            except Exception as e:
                return f"Error communicating with vision model: {e}"

        # Fallback: text-only call (no actual image sent)
        logger.warning("Provider does not support vision; falling back to text-only call")
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"[Attached Image: {path.name}]\n\n{prompt}"}
        ]
        try:
            response = await effective_provider.chat(messages=messages)
            return response.content if response.content else "Vision model returned no description."
        except Exception as e:
            return f"Error communicating with vision model: {e}"
