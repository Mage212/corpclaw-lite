# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from corpclaw_lite.config.providers import ProviderSettings
from corpclaw_lite.llm.base import LLMResponse, Provider, StreamChunk, TokenUsage, ToolCall
from corpclaw_lite.llm.presets import ModelPreset

__all__ = [
    "AnthropicProvider",
]


class AnthropicProvider(Provider):
    """LLM Provider passing through to Anthropic Claude models."""

    _ANTHROPIC_STANDARD_PARAMS = frozenset(
        {
            "model",
            "messages",
            "max_tokens",
            "system",
            "tools",
            "temperature",
            "top_p",
            "top_k",
            "stop_sequences",
            "stream",
            "metadata",
        }
    )

    def __init__(self, settings: ProviderSettings, preset: ModelPreset | None = None):
        self._model = settings.model
        self._preset = preset
        if not settings.api_key:
            raise ValueError("Anthropic requires an API key in settings")

        kwargs: dict[str, Any] = {"api_key": settings.api_key}
        if settings.base_url:
            kwargs["base_url"] = settings.base_url

        self._client = anthropic.AsyncAnthropic(**kwargs)

    def _convert_tool(self, tool: dict[str, Any]) -> dict[str, Any]:
        """Convert standard JSON Schema tool to Anthropic tool schema."""
        # Standard input: {"type": "function", "function": {"name": ..., "parameters": ...}}
        if "function" in tool:
            func = tool["function"]
            return {
                "name": func["name"],
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            }
        return tool

    def _apply_preset(self, system: str | None, kwargs: dict[str, Any]) -> str | None:
        """Merge preset inference params and inject system_prompt_prefix.

        Priority: request-level params > preset params > provider defaults.
        """
        if not self._preset:
            return system

        for k, v in self._preset.inference_params.items():
            if k in self._ANTHROPIC_STANDARD_PARAMS:
                kwargs.setdefault(k, v)

        if self._preset.thinking_budget_tokens:
            budget = self._preset.thinking_budget_tokens
            kwargs.setdefault("max_tokens", budget + 1024)

        if self._preset.system_prompt_prefix:
            prefix = self._preset.system_prompt_prefix
            return f"{prefix}\n{system}" if system else prefix

        return system

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Execute a full chat message and return response with potential tool calls."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        system = self._apply_preset(system, kwargs)
        kwargs.setdefault("max_tokens", 4096)
        if system:
            kwargs["system"] = system

        if tools:
            kwargs["tools"] = [self._convert_tool(t) for t in tools]

        # Call underlying Anthropic SDK
        response = await self._client.messages.create(**kwargs)

        content = ""
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                # Ensure input is a string that represents JSON object for uniformity or dict?
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input,
                    )
                )

        # Build token usage
        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
        )

        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage)

    async def chat_with_image(
        self,
        image_data: str,
        image_media_type: str,
        prompt: str,
        system: str | None = None,
    ) -> LLMResponse:
        """Send a chat request with an inline base64 image."""
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image_media_type,
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        system = self._apply_preset(system, kwargs)
        kwargs.setdefault("max_tokens", 4096)
        if system:
            kwargs["system"] = system

        response = await self._client.messages.create(**kwargs)

        content = ""
        for block in response.content:
            if block.type == "text":
                content += block.text

        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
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
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": 4096,
        }
        if system:
            kwargs["system"] = system

        # Streaming tool usage isn't fully handled by this minimal stream method
        # and wouldn't be as smooth out-of-the-box. Often we just stream text.
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield StreamChunk(content=text)
