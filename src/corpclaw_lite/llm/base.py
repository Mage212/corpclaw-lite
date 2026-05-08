from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "LLMResponse",
    "LLMStreamEvent",
    "LLMStreamStage",
    "Provider",
    "StreamChunk",
    "StreamingProvider",
    "TokenUsage",
    "ToolCall",
    "VisionProvider",
]

LLMStreamStage = Literal[
    "started",
    "reasoning",
    "answer",
    "tool_call",
    "finished",
    "stalled",
    "fallback",
]


class ToolCall(BaseModel):
    """Represents a tool call from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


class TokenUsage(BaseModel):
    """Token usage statistics from an LLM response."""

    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    """Standardized response from an LLM provider."""

    content: str
    reasoning: str = ""
    tool_calls: list[ToolCall] = []
    usage: TokenUsage = Field(default_factory=TokenUsage)


class StreamChunk(BaseModel):
    """A chunk of streamed response."""

    content: str = ""
    reasoning: str = ""
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    tool_call_arguments_delta: str = ""
    finish_reason: str | None = None


class LLMStreamEvent(BaseModel):
    """Backend telemetry event emitted while a full LLM response is streaming."""

    stage: LLMStreamStage
    content_delta: str = ""
    reasoning_delta: str = ""
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    tool_call_arguments_delta: str = ""
    finish_reason: str | None = None
    content_chars: int = 0
    reasoning_chars: int = 0
    tool_call_count: int = 0


@runtime_checkable
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
class StreamingProvider(Protocol):
    """Optional provider capability: stream internally and return a full response."""

    async def chat_streamed(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        on_event: Callable[[LLMStreamEvent], None] | None = None,
    ) -> LLMResponse:
        """Stream a chat request for telemetry, then return a complete response."""
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
