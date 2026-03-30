from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

__all__ = [
    "LLMResponse",
    "Provider",
    "StreamChunk",
    "ToolCall",
    "VisionProvider",
]


class ToolCall(BaseModel):
    """Represents a tool call from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


class LLMResponse(BaseModel):
    """Standardized response from an LLM provider."""

    content: str
    tool_calls: list[ToolCall] = []
    usage: dict[str, int] = {}


class StreamChunk(BaseModel):
    """A chunk of streamed response."""

    content: str = ""


class Provider(Protocol):
    """Protocol for LLM providers."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Send a chat request to the LLM."""
        ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat request from the LLM."""
        ...


@runtime_checkable
class VisionProvider(Protocol):
    """Protocol for providers that support image input."""

    async def chat_with_image(
        self,
        image_data: str,
        image_media_type: str,
        prompt: str,
        system: str | None = None,
    ) -> LLMResponse:
        """Send a chat request with an inline base64 image."""
        ...
