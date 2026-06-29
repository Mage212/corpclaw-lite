# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from corpclaw_lite.config.providers import ProviderSettings
from corpclaw_lite.llm.base import LLMResponse, Provider, StreamChunk, TokenUsage, ToolCall
from corpclaw_lite.llm.presets import ModelPreset, ModelProfile, SamplingProfile

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

    def __init__(
        self,
        settings: ProviderSettings,
        preset: ModelPreset | None = None,
        *,
        model_profile: ModelProfile | None = None,
        sampling: SamplingProfile | None = None,
    ):
        self._model = settings.model
        # Bridge legacy preset → split profiles (D-056), same as OpenAIProvider.
        if model_profile is None and sampling is None and preset is not None:
            from corpclaw_lite.llm.presets import profile_from_legacy_preset

            model_profile, sampling = profile_from_legacy_preset(preset)
        self._preset = preset  # deprecated, kept for back-compat introspection
        self._model_profile = model_profile
        self._sampling = sampling
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

    def _apply_model_profile(self, system: str | None, kwargs: dict[str, Any]) -> str | None:
        """Apply the ModelProfile: default inference params + system_prompt_prefix.

        Lowest inference priority (SamplingProfile / RequestOptions override
        these). Anthropic accepts top_k as a standard param, so all params go
        straight to kwargs (no extra_body split needed, unlike OpenAI).
        """
        if not self._model_profile:
            return system

        for k, v in self._model_profile.default_inference.items():
            if k in self._ANTHROPIC_STANDARD_PARAMS:
                kwargs.setdefault(k, v)

        if self._model_profile.system_prompt_prefix:
            prefix = self._model_profile.system_prompt_prefix
            return f"{prefix}\n{system}" if system else prefix
        return system

    def _apply_sampling(self, kwargs: dict[str, Any]) -> None:
        """Apply the SamplingProfile: thinking mode/budget + inference overrides.

        Middle inference priority (overrides ModelProfile defaults). Note:
        Anthropic has no ``chat_template_kwargs`` control — ``thinking_mode=
        "off"`` is effectively a no-op here (Anthropic reasoning is controlled
        via the dedicated ``thinking`` API param, not yet wired). ``budget``
        caps ``max_tokens`` as a soft signal.
        """
        if not self._sampling:
            return

        # Inference overrides — middle priority, wins over ModelProfile defaults.
        # Direct assignment (not setdefault) so the task-level layer overrides
        # the model-level defaults; per-call RequestOptions override these in turn.
        # Inference overrides — middle priority, wins over ModelProfile defaults.
        # Direct assignment (not setdefault) so the task-level layer overrides
        # the model-level defaults; per-call RequestOptions override these in turn.
        kwargs.update(
            {
                k: v
                for k, v in self._sampling.inference_overrides.items()
                if k in self._ANTHROPIC_STANDARD_PARAMS
            }
        )

        if self._sampling.thinking_mode == "budget" and self._sampling.thinking_budget:
            kwargs["max_tokens"] = self._sampling.thinking_budget + 1024

    def _apply_preset(self, system: str | None, kwargs: dict[str, Any]) -> str | None:
        """DEPRECATED: combined preset application (back-compat bridge)."""
        system = self._apply_model_profile(system, kwargs)
        self._apply_sampling(kwargs)
        return system

    @staticmethod
    def _anthropic_response_summary(
        resp_dict: dict[str, Any] | None,
        content: str,
        tool_calls: list[ToolCall],
        usage: TokenUsage,
    ) -> dict[str, Any]:
        """Build a response summary dict for payload capture.

        Anthropic responses don't have ``reasoning_content``; reasoning is
        conveyed via ``thinking`` content blocks (not separately captured here).
        """
        finish_reason = None
        if resp_dict is not None:
            finish_reason = resp_dict.get("stop_reason")
        return {
            "content": content,
            "reasoning": None,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls
            ],
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            },
            "finish_reason": finish_reason,
        }

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

        # D-056 post-0.2.0: raw request/response capture (no-op when disabled).
        from corpclaw_lite.llm.base import (
            get_capture_session_id,
            get_capture_user_id,
            get_run_id,
        )
        from corpclaw_lite.logging.payload import get_payload_logger

        pl = get_payload_logger()
        if pl is not None and pl.enabled:
            try:
                resp_dict = response.model_dump()
            except Exception:
                resp_dict = None
            pl.capture(
                run_id=get_run_id(),
                user_id=get_capture_user_id(),
                session_id=get_capture_session_id(),
                phase="chat",
                request={
                    "model": kwargs.get("model"),
                    "messages": kwargs.get("messages"),
                    "tools": kwargs.get("tools"),
                    "params": {
                        k: v
                        for k, v in kwargs.items()
                        if k in {"max_tokens", "temperature", "top_p", "stop_sequences", "top_k"}
                    }
                    or None,
                    "extra_body": None,
                },
                response=self._anthropic_response_summary(resp_dict, content, tool_calls, usage),
                finish_reason=getattr(response, "stop_reason", None),
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

        # D-056 post-0.2.0: raw request/response capture (no-op when disabled).
        from corpclaw_lite.llm.base import (
            get_capture_session_id,
            get_capture_user_id,
            get_run_id,
        )
        from corpclaw_lite.logging.payload import get_payload_logger

        pl = get_payload_logger()
        if pl is not None and pl.enabled:
            try:
                resp_dict = response.model_dump()
            except Exception:
                resp_dict = None
            pl.capture(
                run_id=get_run_id(),
                user_id=get_capture_user_id(),
                session_id=get_capture_session_id(),
                phase="chat_with_image",
                request={
                    "model": kwargs.get("model"),
                    "messages": kwargs.get("messages"),
                    "tools": None,
                    "params": {
                        k: v
                        for k, v in kwargs.items()
                        if k in {"max_tokens", "temperature", "top_p", "stop_sequences", "top_k"}
                    }
                    or None,
                    "extra_body": None,
                },
                response=self._anthropic_response_summary(resp_dict, content, [], usage),
                finish_reason=getattr(response, "stop_reason", None),
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
