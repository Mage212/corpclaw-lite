# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import openai

from corpclaw_lite.config.settings import ProviderSettings
from corpclaw_lite.llm.base import LLMResponse, Provider, StreamChunk, ToolCall
from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_call


class OpenAIProvider(Provider):
    """LLM Provider passing through to OpenAI-compatible models."""

    def __init__(self, settings: ProviderSettings):
        self._model = settings.model
        kwargs: dict[str, str] = {}
        if settings.api_key:
            kwargs["api_key"] = settings.api_key
        else:
            kwargs["api_key"] = "dummy"  # needed for local models if client requires it

        if settings.base_url:
            kwargs["base_url"] = settings.base_url

        self._client = openai.AsyncOpenAI(**kwargs)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Execute a full chat message and return response with potential tool calls."""
        # Convert system prompt to a message if provided
        final_messages: list[dict[str, Any]] = []
        if system:
            final_messages.append({"role": "system", "content": system})
        final_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": final_messages,
        }

        if tools:
            # OpenAI format usually accepts tools directly
            kwargs["tools"] = tools

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls: list[ToolCall] = []

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

        usage = {}
        if response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            }

        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat response without parsing tool calls."""
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
