import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from corpclaw_lite.llm.base import Provider
    from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)


class VisionProcessor:
    """Handles image reading via a dedicated LLM call with a vision model."""

    def __init__(self, provider: "Provider") -> None:
        self._provider = provider

    async def describe(self, path: Path, prompt: str, user: "User | None" = None) -> str:
        """Read an image and get a description/analysis from the vision model."""
        if not path.exists() or not path.is_file():
            return f"Error: Image File not found: {path}"

        # In a real implementation this would encode the image as base64
        # and attach it to the message according to provider specs.
        # Anthropic and OpenAI expect different formats (e.g. image_url vs base64 block)

        # For Phase 1 / Phase 2 skeleton, we just mock the vision response
        logger.info(f"VisionProcessor describing image {path} with prompt: {prompt}")

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"[Attached Image: {path.name}]\n\n{prompt}"}
        ]

        try:
            response = await self._provider.chat(messages=messages)
            if response.content:
                return response.content
            return "Vision model returned no text description."
        except Exception as e:
            return f"Error communicating with vision model: {e}"
