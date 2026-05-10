from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "BackendRequestOptions",
    "LLMResponse",
    "LLMStreamEvent",
    "LLMStreamStage",
    "Provider",
    "StreamChunk",
    "StreamingProvider",
    "TokenUsage",
    "ToolCall",
    "VisionProvider",
    "get_backend_request_options",
    "reset_backend_request_options",
    "set_backend_request_options",
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
    cached_input_tokens: int = 0
    prompt_processing_tokens: int = 0
    prompt_processing_ms: float = 0.0
    predicted_tokens: int = 0
    predicted_ms: float = 0.0


@dataclass(frozen=True)
class BackendRequestOptions:
    """Request-local backend-specific options passed through provider transports."""

    extra_body: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


_backend_request_options: contextvars.ContextVar[BackendRequestOptions | None] = (
    contextvars.ContextVar("backend_request_options", default=None)
)


def set_backend_request_options(
    options: BackendRequestOptions | None,
) -> contextvars.Token[BackendRequestOptions | None]:
    """Set backend-specific request options for the current async context."""
    return _backend_request_options.set(options)


def reset_backend_request_options(token: contextvars.Token[BackendRequestOptions | None]) -> None:
    """Reset backend-specific request options to the previous value."""
    _backend_request_options.reset(token)


def get_backend_request_options() -> BackendRequestOptions | None:
    """Return backend-specific request options for the current async context."""
    return _backend_request_options.get()


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
