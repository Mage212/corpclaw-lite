# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import openai

from corpclaw_lite.config.settings import ProviderSettings
from corpclaw_lite.llm.base import LLMResponse, Provider, StreamChunk, TokenUsage, ToolCall
from corpclaw_lite.llm.presets import ModelPreset
from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_call

__all__ = [
    "OpenAIProvider",
]

logger = logging.getLogger(__name__)


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
            e = raw.index(cfg.close_tag)
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
                    return "", [*tool_calls, parse_result.tool_call]
                return reasoning_text.strip(), tool_calls
            return reasoning_text.strip(), tool_calls

        return reasoning_text.strip(), tool_calls

    # ── Main chat ─────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Execute a full chat message and return response with potential tool calls."""
        # Convert system prompt to a message if provided
        final_messages: list[dict[str, Any]] = []

        kwargs: dict[str, Any] = {
            "model": self._model,
        }

        # Apply preset: merge inference params + inject system_prompt_prefix
        system = self._apply_preset(system, kwargs)

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

        logger.debug(
            "[%s] Sending %d messages, roles=%s, tools=%d",
            self._model,
            len(final_messages),
            [m["role"] for m in final_messages],
            len(tools) if tools else 0,
        )

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        # ── Extract reasoning + content ───────────────────────────────────────
        reasoning, content = self._parse_reasoning(choice.message)

        tool_calls: list[ToolCall] = []

        # ── Qwen3 Extended Thinking fallback ──────────────────────────────────
        content, tool_calls = self._resolve_reasoning_fallback(
            content, choice.finish_reason, choice.message, tools, tool_calls
        )

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                # Arguments are returned as string from OpenAI
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
        elif tools and content:
            # XML Fallback check for local models
            allowed_names = {t["function"]["name"] for t in tools if "function" in t}
            parse_result = parse_xml_tool_call(content, allowed_tool_names=allowed_names)
            if parse_result.tool_call:
                tool_calls.append(parse_result.tool_call)

        usage = TokenUsage()
        if response.usage:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
            )

        return LLMResponse(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            usage=usage,
        )

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
            extra_body: dict[str, Any] = {}
            for k, v in self._preset.inference_params.items():
                if k in self._OPENAI_STANDARD_PARAMS:
                    kwargs.setdefault(k, v)
                else:
                    extra_body.setdefault(k, v)
            if extra_body:
                kwargs["extra_body"] = extra_body

        if system:
            messages.insert(0, {"role": "system", "content": system})

        kwargs["messages"] = messages

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        content = choice.message.content or ""

        usage = TokenUsage()
        if response.usage:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
            )
        return LLMResponse(content=content, usage=usage)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat response without parsing tool calls.

        Note: Model presets (inference_params, thinking_budget, system_prompt_prefix)
        are NOT applied to streaming requests. Streaming is intended for cloud-hosted
        models that don't require preset-based parameter tuning.
        """
        final_messages: list[dict[str, Any]] = []
        if system:
            final_messages.append({"role": "system", "content": system})
        final_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": final_messages,
            "stream": True,
        }

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:  # type: ignore
            if chunk.choices and chunk.choices[0].delta.content:
                yield StreamChunk(content=chunk.choices[0].delta.content)
