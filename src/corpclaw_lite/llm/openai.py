# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import openai

from corpclaw_lite.config.providers import ProviderSettings
from corpclaw_lite.llm.base import (
    LLMResponse,
    LLMStreamEvent,
    Provider,
    StreamChunk,
    TokenUsage,
    ToolCall,
    get_backend_request_options,
)
from corpclaw_lite.llm.presets import ModelPreset
from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_call

__all__ = [
    "OpenAIProvider",
]

logger = logging.getLogger(__name__)


def _text_delta(value: Any) -> str:
    """Return provider delta text only when it is actually a string."""
    return value if isinstance(value, str) else ""


def _raw_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _raw_int(value: Any, key: str, default: int = 0) -> int:
    raw = _raw_get(value, key, default)
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int | float | str):
        try:
            return int(raw)
        except ValueError:
            return default
    return default


def _raw_float(value: Any, key: str, default: float = 0.0) -> float:
    raw = _raw_get(value, key, default)
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, int | float | str):
        try:
            return float(raw)
        except ValueError:
            return default
    return default


def _usage_from_raw(raw_usage: Any) -> TokenUsage:
    if not raw_usage:
        return TokenUsage()
    details = _raw_get(raw_usage, "prompt_tokens_details", None) or {}
    cached_tokens = _raw_int(details, "cached_tokens")
    input_tokens = _raw_int(raw_usage, "prompt_tokens")
    output_tokens = _raw_int(raw_usage, "completion_tokens")
    total_tokens = _raw_int(raw_usage, "total_tokens")
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens or input_tokens + output_tokens,
        cached_input_tokens=cached_tokens,
    )


def _apply_timings(usage: TokenUsage, raw_timings: Any) -> TokenUsage:
    if not raw_timings:
        return usage
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        prompt_processing_tokens=_raw_int(raw_timings, "prompt_n"),
        prompt_processing_ms=_raw_float(raw_timings, "prompt_ms"),
        predicted_tokens=_raw_int(raw_timings, "predicted_n"),
        predicted_ms=_raw_float(raw_timings, "predicted_ms"),
    )


def _enable_stream_usage(kwargs: dict[str, Any]) -> None:
    stream_options: dict[str, Any] = dict(kwargs.get("stream_options") or {})
    stream_options["include_usage"] = True
    kwargs["stream_options"] = stream_options


class OpenAIProvider(Provider):
    """LLM Provider passing through to OpenAI-compatible models."""

    def __init__(
        self,
        settings: ProviderSettings,
        preset: ModelPreset | None = None,
    ):
        self._model = settings.model
        self._preset = preset
        api_key = settings.api_key or "dummy"  # local models may not need a real key
        if settings.base_url:
            self._client = openai.AsyncOpenAI(api_key=api_key, base_url=settings.base_url)
        else:
            self._client = openai.AsyncOpenAI(api_key=api_key)

    # OpenAI SDK accepts these top-level params in chat.completions.create().
    # Everything else (top_k, min_p, repeat_penalty, etc.) must go into
    # extra_body — the SDK merges it into the JSON body, so LM Studio / vLLM
    # receive them without the SDK rejecting them client-side.
    _OPENAI_STANDARD_PARAMS = frozenset(
        {
            "temperature",
            "max_tokens",
            "max_completion_tokens",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "stop",
            "n",
            "seed",
            "stream",
            "logit_bias",
            "logprobs",
            "top_logprobs",
            "user",
            "response_format",
            "tool_choice",
            "parallel_tool_calls",
        }
    )

    def _apply_preset(self, system: str | None, kwargs: dict[str, Any]) -> str | None:
        """Merge preset inference params and inject system_prompt_prefix.

        Priority: request-level params > preset params > provider defaults.
        Uses ``setdefault`` so request-level values are never overwritten.

        Non-standard params (top_k, min_p, etc.) are routed to ``extra_body``
        so the OpenAI SDK doesn't reject them.
        """
        if not self._preset:
            return system

        extra_body: dict[str, Any] = dict(kwargs.pop("extra_body", None) or {})

        # 1. Merge inference params — split standard vs extended
        for k, v in self._preset.inference_params.items():
            if k in self._OPENAI_STANDARD_PARAMS:
                kwargs.setdefault(k, v)
            else:
                extra_body.setdefault(k, v)

        if extra_body:
            kwargs["extra_body"] = extra_body

        # 2. Thinking budget → cap max_tokens
        if self._preset.thinking_budget_tokens:
            budget = self._preset.thinking_budget_tokens
            kwargs.setdefault("max_tokens", budget + 1024)

        # 3. System prompt prefix injection
        if self._preset.system_prompt_prefix:
            prefix = self._preset.system_prompt_prefix
            return f"{prefix}\n{system}" if system else prefix

        return system

    def _parse_reasoning(self, message: Any) -> tuple[str, str]:
        """Extract (reasoning, content) based on preset thinking config.

        Returns:
            (reasoning_text, clean_content) — reasoning is empty string if
            no thinking config is set or no reasoning was found.
        """
        if not self._preset or not self._preset.thinking:
            # No preset / no thinking config — return content as-is
            return "", message.content or ""

        cfg = self._preset.thinking
        if cfg.source == "native":
            # Qwen3-style: API returns reasoning in a dedicated field
            reasoning = getattr(message, "reasoning_content", None) or ""
            return reasoning, message.content or ""

        # source == "content" — parse tags from content (Gemma4-style)
        raw = message.content or ""
        if cfg.open_tag in raw and cfg.close_tag in raw:
            s = raw.index(cfg.open_tag) + len(cfg.open_tag)
            e = raw.index(cfg.close_tag, s)
            reasoning = raw[s:e].strip()
            content = raw[e + len(cfg.close_tag) :].strip()
            return reasoning, content

        return "", raw

    def _resolve_reasoning_fallback(
        self,
        content: str,
        finish_reason: str | None,
        raw_message: Any,
        tools: list[dict[str, Any]] | None,
        tool_calls: list[ToolCall],
    ) -> tuple[str, list[ToolCall]]:
        """Handle Qwen3/LM Studio edge case: response stuck in reasoning_content.

        LM Studio + Qwen3: the model sometimes puts everything into
        reasoning_content and leaves content + tool_calls empty.
        Two sub-cases:
          a) reasoning_content has XML tool calls → parse them (NOT a final answer)
          b) reasoning_content is plain text → use as the final answer

        Returns updated (content, tool_calls). Unchanged if no fallback needed.
        """
        # Only trigger when content is empty, no native tool calls, and stop
        if content or getattr(raw_message, "tool_calls", None) or finish_reason != "stop":
            return content, tool_calls

        reasoning_text: str = getattr(raw_message, "reasoning_content", None) or ""
        if not reasoning_text:
            return content, tool_calls

        if tools:
            _MARKERS = ("<tool_call>", "<function=")
            if any(m in reasoning_text for m in _MARKERS):
                allowed_names = {t["function"]["name"] for t in tools if "function" in t}
                parse_result = parse_xml_tool_call(reasoning_text, allowed_tool_names=allowed_names)
                if parse_result.tool_call:
                    logger.info(
                        "XML fallback (reasoning_content): parsed tool_call %s",
                        parse_result.tool_call.name,
                    )
                    return "", [*tool_calls, parse_result.tool_call]
                logger.debug("XML fallback (reasoning_content): no tool_call parsed from reasoning")
                return reasoning_text.strip(), tool_calls
            return reasoning_text.strip(), tool_calls

        return reasoning_text.strip(), tool_calls

    # ── Main chat ─────────────────────────────────────────────────────────────

    def _build_chat_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Build OpenAI-compatible chat kwargs shared by full and streamed calls."""
        final_messages: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"model": self._model}

        # Apply preset: merge inference params + inject system_prompt_prefix
        system = self._apply_preset(system, kwargs)
        self._apply_backend_options(kwargs)

        if system:
            final_messages.append({"role": "system", "content": system})
        final_messages.extend(messages)

        # Defensive: ensure no None content in any message (breaks Jinja templates)
        for msg in final_messages:
            if msg.get("content") is None:
                msg["content"] = ""

        kwargs["messages"] = final_messages

        if tools:
            # OpenAI format usually accepts tools directly
            kwargs["tools"] = tools

        return kwargs, final_messages

    def _apply_backend_options(self, kwargs: dict[str, Any]) -> None:
        """Merge request-local backend-specific options into OpenAI kwargs."""
        options = get_backend_request_options()
        if options is None or not options.extra_body:
            return
        extra_body: dict[str, Any] = dict(kwargs.pop("extra_body", None) or {})
        extra_body.update(options.extra_body)
        kwargs["extra_body"] = extra_body

    def _tool_calls_from_native(self, native_tool_calls: Any) -> list[ToolCall]:
        """Normalize OpenAI SDK tool calls into our ToolCall model."""
        tool_calls: list[ToolCall] = []
        if not native_tool_calls:
            return tool_calls
        for tc in native_tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {"__raw__": tc.function.arguments}

            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                )
            )
        return tool_calls

    def _finalize_response(
        self,
        *,
        content: str,
        reasoning: str,
        raw_message: Any,
        finish_reason: str | None,
        tools: list[dict[str, Any]] | None,
        usage: TokenUsage,
    ) -> LLMResponse:
        """Apply the same post-processing to full and streamed responses."""
        tool_calls = self._tool_calls_from_native(getattr(raw_message, "tool_calls", None))

        content, tool_calls = self._resolve_reasoning_fallback(
            content, finish_reason, raw_message, tools, tool_calls
        )

        if not tool_calls and tools and content:
            allowed_names = {t["function"]["name"] for t in tools if "function" in t}
            parse_result = parse_xml_tool_call(content, allowed_tool_names=allowed_names)
            if parse_result.tool_call:
                logger.info(
                    "XML fallback (content): parsed tool_call %s",
                    parse_result.tool_call.name,
                )
                tool_calls.append(parse_result.tool_call)

        return LLMResponse(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            usage=usage,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Execute a full chat message and return response with potential tool calls."""
        kwargs, final_messages = self._build_chat_kwargs(messages, tools, system)

        logger.debug(
            "[%s] Sending %d messages, roles=%s, tools=%d",
            self._model,
            len(final_messages),
            [m["role"] for m in final_messages],
            len(tools) if tools else 0,
        )

        response = await self._client.chat.completions.create(**kwargs)
        if not response.choices:
            return LLMResponse(content="", tool_calls=[], reasoning="")
        choice = response.choices[0]

        # ── Extract reasoning + content ───────────────────────────────────────
        reasoning, content = self._parse_reasoning(choice.message)

        usage = _usage_from_raw(response.usage)
        usage = _apply_timings(usage, getattr(response, "timings", None))

        return self._finalize_response(
            content=content,
            reasoning=reasoning,
            raw_message=choice.message,
            finish_reason=choice.finish_reason,
            tools=tools,
            usage=usage,
        )

    async def chat_streamed(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        on_event: Callable[[LLMStreamEvent], None] | None = None,
    ) -> LLMResponse:
        """Stream for backend telemetry, then return a complete LLMResponse."""
        kwargs, final_messages = self._build_chat_kwargs(messages, tools, system)
        kwargs["stream"] = True
        _enable_stream_usage(kwargs)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, str | None]] = {}
        finish_reason: str | None = None
        usage = TokenUsage()

        def emit(event: LLMStreamEvent) -> None:
            if on_event is not None:
                on_event(event)

        emit(LLMStreamEvent(stage="started"))

        logger.debug(
            "[%s] Streaming %d messages, roles=%s, tools=%d",
            self._model,
            len(final_messages),
            [m["role"] for m in final_messages],
            len(tools) if tools else 0,
        )

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:  # type: ignore
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage:
                usage = _usage_from_raw(chunk_usage)
            chunk_timings = getattr(chunk, "timings", None)
            if chunk_timings:
                usage = _apply_timings(usage, chunk_timings)
            if not getattr(chunk, "choices", None):
                continue

            choice = chunk.choices[0]
            choice_finish_reason = _text_delta(getattr(choice, "finish_reason", None))
            finish_reason = choice_finish_reason or finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            reasoning_delta = _text_delta(getattr(delta, "reasoning_content", None))
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                emit(
                    LLMStreamEvent(
                        stage="reasoning",
                        reasoning_delta=reasoning_delta,
                        reasoning_chars=sum(len(part) for part in reasoning_parts),
                        content_chars=sum(len(part) for part in content_parts),
                        tool_call_count=len(tool_parts),
                    )
                )

            content_delta = _text_delta(getattr(delta, "content", None))
            if content_delta:
                content_parts.append(content_delta)
                emit(
                    LLMStreamEvent(
                        stage="answer",
                        content_delta=content_delta,
                        reasoning_chars=sum(len(part) for part in reasoning_parts),
                        content_chars=sum(len(part) for part in content_parts),
                        tool_call_count=len(tool_parts),
                    )
                )

            for tc in getattr(delta, "tool_calls", None) or []:
                idx = int(getattr(tc, "index", 0) or 0)
                part = tool_parts.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if getattr(tc, "id", None):
                    part["id"] = tc.id
                fn = getattr(tc, "function", None)
                name_delta = _text_delta(getattr(fn, "name", None)) if fn is not None else ""
                args_delta = _text_delta(getattr(fn, "arguments", None)) if fn is not None else ""
                if name_delta:
                    part["name"] = name_delta
                if args_delta:
                    part["arguments"] = f"{part['arguments'] or ''}{args_delta}"
                emit(
                    LLMStreamEvent(
                        stage="tool_call",
                        tool_call_id=part["id"],
                        tool_call_name=part["name"],
                        tool_call_arguments_delta=args_delta or "",
                        reasoning_chars=sum(len(part) for part in reasoning_parts),
                        content_chars=sum(len(part) for part in content_parts),
                        tool_call_count=len(tool_parts),
                    )
                )

        native_tool_calls: list[Any] = []
        for idx in sorted(tool_parts):
            part = tool_parts[idx]
            name = part["name"]
            if not name:
                continue
            native_tool_calls.append(
                SimpleNamespace(
                    id=part["id"] or f"call_{idx}",
                    function=SimpleNamespace(
                        name=name,
                        arguments=part["arguments"] or "{}",
                    ),
                )
            )

        content = "".join(content_parts)
        raw_message = SimpleNamespace(
            content=content,
            reasoning_content="".join(reasoning_parts),
            tool_calls=native_tool_calls,
        )
        reasoning, content = self._parse_reasoning(raw_message)
        if not reasoning and raw_message.reasoning_content:
            reasoning = raw_message.reasoning_content
        response = self._finalize_response(
            content=content,
            reasoning=reasoning,
            raw_message=raw_message,
            finish_reason=finish_reason,
            tools=tools,
            usage=usage,
        )
        emit(
            LLMStreamEvent(
                stage="finished",
                finish_reason=finish_reason,
                reasoning_chars=len(response.reasoning or ""),
                content_chars=len(response.content or ""),
                tool_call_count=len(response.tool_calls or []),
            )
        )
        return response

    async def chat_with_image(
        self,
        image_data: str,
        image_media_type: str,
        prompt: str,
        system: str | None = None,
    ) -> LLMResponse:
        """Send a chat request with an inline base64 image (OpenAI format)."""
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image_media_type};base64,{image_data}",
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        kwargs: dict[str, Any] = {"model": self._model}

        # Apply preset inference params (but NOT system_prompt_prefix for vision).
        # Non-standard params (top_k, min_p, etc.) go into extra_body.
        if self._preset:
            # Thinking budget → cap max_tokens (same logic as chat())
            if self._preset.thinking_budget_tokens:
                budget = self._preset.thinking_budget_tokens
                kwargs.setdefault("max_tokens", budget + 1024)
            extra_body: dict[str, Any] = {}
            for k, v in self._preset.inference_params.items():
                if k in self._OPENAI_STANDARD_PARAMS:
                    kwargs.setdefault(k, v)
                else:
                    extra_body.setdefault(k, v)
            if extra_body:
                kwargs["extra_body"] = extra_body
        self._apply_backend_options(kwargs)

        if system:
            messages.insert(0, {"role": "system", "content": system})

        kwargs["messages"] = messages

        response = await self._client.chat.completions.create(**kwargs)
        if not response.choices:
            return LLMResponse(content="")
        choice = response.choices[0]
        content = choice.message.content or ""

        usage = _usage_from_raw(response.usage)
        usage = _apply_timings(usage, getattr(response, "timings", None))
        return LLMResponse(content=content, usage=usage)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat response without parsing tool calls.

        This is a text-streaming convenience method. Agent orchestration should
        use ``chat_streamed()`` so tool calls and reasoning can be reconstructed.
        """
        kwargs, _final_messages = self._build_chat_kwargs(messages, tools, system)
        kwargs["stream"] = True
        _enable_stream_usage(kwargs)

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:  # type: ignore
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            yield StreamChunk(
                content=_text_delta(getattr(delta, "content", None)),
                reasoning=_text_delta(getattr(delta, "reasoning_content", None)),
                finish_reason=_text_delta(getattr(choice, "finish_reason", None)) or None,
            )
