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
    "get_run_id",
    "reset_backend_request_options",
    "reset_request_options",
    "reset_run_id",
    "set_backend_request_options",
    "set_request_options",
    "set_run_id",
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


# ── Run-id contextvar (D-056 post-0.2.0: payload capture) ──────────────────────
# Set by AgentLoop.run() so providers can tag raw-payload captures with the
# originating run_id without threading it through every call signature.
# Defaults to None — capture still works without it (run_id field is null).
_run_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_run_id", default=None)


def set_run_id(run_id: str | None) -> contextvars.Token[str | None]:
    """Set the current agent run id for payload-capture tagging."""
    return _run_id_ctx.set(run_id)


def reset_run_id(token: contextvars.Token[str | None]) -> None:
    """Reset the run-id contextvar."""
    _run_id_ctx.reset(token)


def get_run_id() -> str | None:
    """Return the current agent run id, or None if not in a run context."""
    return _run_id_ctx.get()


# ── Capture-correlation contextvars (B-063 S4) ──────────────────────────────
# Set by AgentLoop.run() so providers can tag raw-payload captures with the
# originating user_id + session_id without threading them through call
# signatures. Mirrors the run_id pattern above.
_capture_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_capture_user_id", default=None
)
_capture_session_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "llm_capture_session_id", default=None
)


def set_capture_context(
    user_id: str | None, session_id: int | None
) -> tuple[contextvars.Token[str | None], contextvars.Token[int | None]]:
    """Bind user_id + session_id for payload-capture tagging (B-063 S4)."""
    return (_capture_user_id.set(user_id), _capture_session_id.set(session_id))


def reset_capture_context(
    tokens: tuple[contextvars.Token[str | None], contextvars.Token[int | None]],
) -> None:
    """Reset the capture-correlation contextvars."""
    _capture_user_id.reset(tokens[0])
    _capture_session_id.reset(tokens[1])


def get_capture_user_id() -> str | None:
    """Return the user_id bound for capture, or None."""
    return _capture_user_id.get()


def get_capture_session_id() -> int | None:
    """Return the session_id bound for capture, or None."""
    return _capture_session_id.get()


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
