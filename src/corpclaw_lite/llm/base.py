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
    "RequestOptions",
    "StreamChunk",
    "StreamingProvider",
    "ThinkingOverride",
    "TokenUsage",
    "ToolCall",
    "VisionProvider",
    "get_backend_request_options",
    "get_request_options",
    "reset_backend_request_options",
    "reset_request_options",
    "set_backend_request_options",
    "set_request_options",
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
    total_tokens: int = 0
    cached_input_tokens: int = 0
    prompt_processing_tokens: int = 0
    prompt_processing_ms: float = 0.0
    predicted_tokens: int = 0
    predicted_ms: float = 0.0

    def model_post_init(self, __context: Any) -> None:
        """Fill total_tokens for providers that only report prompt/completion tokens."""
        if self.total_tokens == 0 and (self.input_tokens or self.output_tokens):
            self.total_tokens = self.input_tokens + self.output_tokens


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


@dataclass(frozen=True)
class ThinkingOverride:
    """Per-call override of model thinking/reasoning behaviour.

    Independent of :class:`ModelProfile`/SamplingProfile: it overrides whatever
    the routing-resolved profiles would produce, for a single LLM call.

    Modes:
        default:  the model's natural thinking (do not override).
        off:      disable thinking entirely (e.g. ``chat_template_kwargs.
                  enable_thinking=false`` for Qwen/gemma, or no ``<|think|>``
                  prefix for gemma4). Fast extraction-style calls.
        budget:   soft-cap reasoning output to ``budget`` tokens (sets
                  ``thinking_budget_tokens`` semantics, model-dependent).
    """

    mode: Literal["default", "off", "budget"] = "default"
    budget: int | None = None


@dataclass(frozen=True)
class RequestOptions:
    """Per-call LLM request overrides, set via an async-context contextvar.

    This is the second, independent rail next to :class:`BackendRequestOptions`.
    ``BackendRequestOptions`` carries transport-level keys (``id_slot``,
    ``cache_prompt``) set by the LLM queue/cache layer; ``RequestOptions``
    carries inference + thinking overrides set per-call (e.g. by PhasePolicy).
    Both are merged by the provider in ``_build_chat_kwargs`` with deterministic
    priority::

        model_profile defaults  (lowest)
          < SamplingProfile overrides
            < RequestOptions.inference / RequestOptions.thinking
              (per-call, highest among inference)
            < BackendRequestOptions.extra_body
              (transport, lowest of its own layer)

    Merge order is implemented in ``OpenAIProvider._build_chat_kwargs``; see
    ``tests/test_request_options.py`` for the contract.

    All fields are optional — ``None`` means "do not override".
    """

    inference: dict[str, Any] | None = None
    thinking: ThinkingOverride | None = None


_call_options: contextvars.ContextVar[RequestOptions | None] = contextvars.ContextVar(
    "llm_call_options", default=None
)


def set_request_options(
    options: RequestOptions | None,
) -> contextvars.Token[RequestOptions | None]:
    """Set per-call LLM request overrides for the current async context.

    Use as a context manager replacement::

        token = set_request_options(RequestOptions(thinking=ThinkingOverride(mode="off")))
        try:
            response = await provider.chat(...)
        finally:
            reset_request_options(token)

    Independent of :func:`set_backend_request_options` — both contextvars may
    be active simultaneously and are merged by the provider.
    """
    return _call_options.set(options)


def reset_request_options(token: contextvars.Token[RequestOptions | None]) -> None:
    """Reset per-call request overrides to the previous value."""
    _call_options.reset(token)


def get_request_options() -> RequestOptions | None:
    """Return per-call request overrides for the current async context."""
    return _call_options.get()


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
