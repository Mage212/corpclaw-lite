# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic

from corpclaw_lite.config.providers import ProviderSettings
from corpclaw_lite.llm.base import (
    LLMResponse,
    LLMStreamEvent,
    Provider,
    StreamChunk,
    TokenUsage,
    ToolCall,
    get_capture_session_id,
    get_capture_user_id,
    get_request_options,
    get_run_id,
)
from corpclaw_lite.llm.presets import ModelPreset, ModelProfile, SamplingProfile
from corpclaw_lite.logging.trace import log_event

__all__ = ["AnthropicProvider"]

logger = logging.getLogger(__name__)


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


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class AnthropicProvider(Provider):
    """Anthropic provider with OpenAI-history and streaming protocol parity."""

    _ANTHROPIC_STANDARD_PARAMS = frozenset(
        {
            "max_tokens",
            "temperature",
            "top_p",
            "top_k",
            "stop_sequences",
            "thinking",
        }
    )
    _MAX_STREAM_TEXT_CHARS = 1_000_000
    _MAX_STREAM_TOOL_ARGUMENT_CHARS = 262_144
    _MAX_STREAM_TOTAL_TOOL_ARGUMENT_CHARS = 1_000_000
    _MAX_STREAM_TOOL_CALLS = 128

    def __init__(
        self,
        settings: ProviderSettings,
        preset: ModelPreset | None = None,
        *,
        model_profile: ModelProfile | None = None,
        sampling: SamplingProfile | None = None,
    ):
        self._model = settings.model
        if model_profile is None and sampling is None and preset is not None:
            from corpclaw_lite.llm.presets import profile_from_legacy_preset

            model_profile, sampling = profile_from_legacy_preset(preset)
        self._preset = preset
        self._model_profile = model_profile
        self._sampling = sampling
        if not settings.api_key:
            raise ValueError("Anthropic requires an API key in settings")

        client_kwargs: dict[str, Any] = {"api_key": settings.api_key}
        if settings.base_url:
            client_kwargs["base_url"] = settings.base_url
        self._client = anthropic.AsyncAnthropic(**client_kwargs)

    @staticmethod
    def _convert_tool(tool: dict[str, Any]) -> dict[str, Any]:
        if "function" not in tool:
            return dict(tool)
        function = tool["function"]
        return {
            "name": function["name"],
            "description": function.get("description", ""),
            "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
        }

    @staticmethod
    def _content_blocks(content: Any) -> list[dict[str, Any]]:
        if content is None or content == "":
            return []
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if isinstance(content, list):
            return [dict(block) if isinstance(block, dict) else block for block in content]
        return [{"type": "text", "text": str(content)}]

    @staticmethod
    def _append_message(
        converted: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
    ) -> None:
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            prior = converted[-1]["content"]
            if not isinstance(prior, list):
                prior = [{"type": "text", "text": str(prior)}]
                converted[-1]["content"] = prior
            prior.extend(blocks)
            return
        converted.append({"role": role, "content": blocks})

    @staticmethod
    def _tool_call_fields(tool_call: Any) -> tuple[str, str, dict[str, Any]]:
        call_id = str(_raw_get(tool_call, "id", ""))
        function = _raw_get(tool_call, "function", {})
        name = str(_raw_get(function, "name", ""))
        arguments = _raw_get(function, "arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Malformed tool arguments for {name or '<unknown>'}") from exc
        if not isinstance(arguments, dict):
            raise ValueError(f"Tool arguments for {name or '<unknown>'} must be an object")
        if not call_id or not name:
            raise ValueError("Assistant tool call requires non-empty id and name")
        return call_id, name, arguments

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate canonical OpenAI history into Anthropic content blocks.

        Consecutive user messages (including tool results) are deliberately
        coalesced because Anthropic requires alternating user/assistant turns.
        """
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                raise ValueError("System messages must be passed via the system argument")
            if role == "user":
                self._append_message(
                    converted, "user", self._content_blocks(message.get("content"))
                )
                continue
            if role == "assistant":
                blocks = self._content_blocks(message.get("content"))
                for tool_call in message.get("tool_calls") or []:
                    call_id, name, arguments = self._tool_call_fields(tool_call)
                    blocks.append(
                        {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
                    )
                self._append_message(converted, "assistant", blocks)
                continue
            if role == "tool":
                tool_use_id = message.get("tool_call_id")
                if not isinstance(tool_use_id, str) or not tool_use_id:
                    raise ValueError("Tool result requires a non-empty tool_call_id")
                content = message.get("content", "")
                result_text = content if isinstance(content, str) else json.dumps(content)
                block = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_text,
                    "is_error": result_text.lstrip().lower().startswith("error:"),
                }
                self._append_message(converted, "user", [block])
                continue
            raise ValueError(f"Unsupported message role for Anthropic: {role!r}")
        return converted

    def _effective_thinking(self) -> tuple[str, int | None]:
        mode = self._sampling.thinking_mode if self._sampling else "default"
        budget = self._sampling.thinking_budget if self._sampling else None
        options = get_request_options()
        if options and options.thinking:
            mode = options.thinking.mode
            budget = options.thinking.budget
        return mode, budget

    def _build_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> dict[str, Any]:
        """Build every Anthropic request with deterministic configuration priority."""
        kwargs: dict[str, Any] = {"model": self._model}

        if self._model_profile:
            for key, value in self._model_profile.default_inference.items():
                if key in self._ANTHROPIC_STANDARD_PARAMS:
                    kwargs.setdefault(key, value)
        if self._sampling:
            kwargs.update(
                {
                    key: value
                    for key, value in self._sampling.inference_overrides.items()
                    if key in self._ANTHROPIC_STANDARD_PARAMS
                }
            )

        options = get_request_options()
        if options and options.inference:
            for key, value in options.inference.items():
                if key in self._ANTHROPIC_STANDARD_PARAMS:
                    kwargs[key] = value
                else:
                    logger.warning("Ignoring unsupported Anthropic inference option: %s", key)

        mode, budget = self._effective_thinking()
        if mode == "off":
            kwargs["thinking"] = {"type": "disabled"}
        elif mode == "budget":
            if budget is None or budget < 1024:
                raise ValueError("Anthropic thinking budget must be at least 1024 tokens")
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            if not (options and options.inference and "max_tokens" in options.inference):
                kwargs["max_tokens"] = budget + 1024
            if int(kwargs.get("max_tokens", 0)) <= budget:
                raise ValueError("Anthropic max_tokens must exceed thinking budget")
        else:
            kwargs.pop("thinking", None)

        kwargs.setdefault("max_tokens", 4096)
        final_system = system
        if mode != "off" and self._model_profile and self._model_profile.system_prompt_prefix:
            prefix = self._model_profile.system_prompt_prefix
            final_system = f"{prefix}\n{system}" if system else prefix
        if final_system:
            kwargs["system"] = final_system

        kwargs["messages"] = self._convert_messages(messages)
        if tools:
            kwargs["tools"] = [self._convert_tool(tool) for tool in tools]
        return kwargs

    # Retained for callers/tests that used the old helper surface.
    def _apply_preset(self, system: str | None, kwargs: dict[str, Any]) -> str | None:
        built = self._build_request([], system=system)
        kwargs.update(
            {key: value for key, value in built.items() if key not in {"messages", "system"}}
        )
        return built.get("system")

    @staticmethod
    def _usage_from_raw(raw_usage: Any) -> TokenUsage:
        input_tokens = _raw_int(raw_usage, "input_tokens")
        output_tokens = _raw_int(raw_usage, "output_tokens")
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cached_input_tokens=_raw_int(raw_usage, "cache_read_input_tokens"),
        )

    @staticmethod
    def _allowed_tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
        names: set[str] = set()
        for tool in tools or []:
            function = tool.get("function")
            if isinstance(function, dict) and function.get("name"):
                names.add(str(function["name"]))
            elif tool.get("name"):
                names.add(str(tool["name"]))
        return names

    def _parse_response(self, response: Any, tools: list[dict[str, Any]] | None) -> LLMResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        allowed_names = self._allowed_tool_names(tools)
        for block in _raw_get(response, "content", []) or []:
            block_type = _raw_get(block, "type")
            if block_type == "text":
                content_parts.append(_text(_raw_get(block, "text")))
            elif block_type == "thinking":
                reasoning_parts.append(_text(_raw_get(block, "thinking")))
            elif block_type == "tool_use":
                name = str(_raw_get(block, "name", ""))
                if name not in allowed_names:
                    logger.warning("Rejected Anthropic tool call outside offered schema: %s", name)
                    log_event(
                        "native_tool_call_rejected",
                        get_run_id() or "unknown",
                        tool=name,
                        reason="not_in_offered_schema",
                    )
                    continue
                arguments = _raw_get(block, "input", {})
                if not isinstance(arguments, dict):
                    logger.warning("Rejected Anthropic tool call with non-object input: %s", name)
                    continue
                tool_calls.append(
                    ToolCall(id=str(_raw_get(block, "id", "")), name=name, arguments=arguments)
                )
        return LLMResponse(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            usage=self._usage_from_raw(_raw_get(response, "usage")),
        )

    def _capture(
        self,
        phase: str,
        kwargs: dict[str, Any],
        response: LLMResponse,
        *,
        finish_reason: str | None,
    ) -> None:
        from corpclaw_lite.logging.payload import get_payload_logger

        payload_logger = get_payload_logger()
        if payload_logger is None or not payload_logger.enabled:
            return
        payload_logger.capture(
            run_id=get_run_id(),
            user_id=get_capture_user_id(),
            session_id=get_capture_session_id(),
            phase=phase,
            request={
                "model": kwargs.get("model"),
                "messages": kwargs.get("messages"),
                "tools": kwargs.get("tools"),
                "params": {
                    key: value
                    for key, value in kwargs.items()
                    if key in self._ANTHROPIC_STANDARD_PARAMS
                }
                or None,
                "extra_body": None,
            },
            response={
                "content": response.content,
                "reasoning": response.reasoning,
                "tool_calls": [call.model_dump() for call in response.tool_calls],
                "usage": response.usage.model_dump(),
                "finish_reason": finish_reason,
            },
            finish_reason=finish_reason,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        kwargs = self._build_request(messages, tools, system)
        raw_response = await self._client.messages.create(**kwargs)
        response = self._parse_response(raw_response, tools)
        self._capture("chat", kwargs, response, finish_reason=_raw_get(raw_response, "stop_reason"))
        return response

    async def chat_with_image(
        self,
        image_data: str,
        image_media_type: str,
        prompt: str,
        system: str | None = None,
    ) -> LLMResponse:
        messages = [
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
        kwargs = self._build_request(messages, system=system)
        raw_response = await self._client.messages.create(**kwargs)
        response = self._parse_response(raw_response, None)
        self._capture(
            "chat_with_image",
            kwargs,
            response,
            finish_reason=_raw_get(raw_response, "stop_reason"),
        )
        return response

    @staticmethod
    def _emit(callback: Callable[[LLMStreamEvent], None] | None, event: LLMStreamEvent) -> None:
        if callback is not None:
            callback(event)

    def _check_stream_bound(self, current: int, delta: str, limit: int, field: str) -> int:
        updated = current + len(delta)
        if updated > limit:
            raise ValueError(f"Anthropic streamed {field} exceeded the safe accumulation limit")
        return updated

    async def chat_streamed(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        on_event: Callable[[LLMStreamEvent], None] | None = None,
    ) -> LLMResponse:
        kwargs = self._build_request(messages, tools, system)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        content_chars = 0
        reasoning_chars = 0
        tool_argument_chars = 0
        usage = TokenUsage()
        finish_reason: str | None = None
        self._emit(on_event, LLMStreamEvent(stage="started"))

        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                event_type = _raw_get(event, "type")
                if event_type == "message_start":
                    usage = self._usage_from_raw(_raw_get(_raw_get(event, "message"), "usage"))
                    continue
                if event_type == "content_block_start":
                    index = int(_raw_get(event, "index", 0))
                    block = _raw_get(event, "content_block")
                    if _raw_get(block, "type") == "tool_use":
                        if (
                            index not in tool_parts
                            and len(tool_parts) >= self._MAX_STREAM_TOOL_CALLS
                        ):
                            raise ValueError(
                                "Anthropic streamed tool calls exceeded the safe accumulation limit"
                            )
                        tool_parts[index] = {
                            "id": str(_raw_get(block, "id", "")),
                            "name": str(_raw_get(block, "name", "")),
                            "arguments": "",
                        }
                        self._emit(
                            on_event,
                            LLMStreamEvent(
                                stage="tool_call",
                                tool_call_id=tool_parts[index]["id"],
                                tool_call_name=tool_parts[index]["name"],
                                content_chars=content_chars,
                                reasoning_chars=reasoning_chars,
                                tool_call_count=len(tool_parts),
                            ),
                        )
                    continue
                if event_type == "content_block_delta":
                    index = int(_raw_get(event, "index", 0))
                    delta = _raw_get(event, "delta")
                    delta_type = _raw_get(delta, "type")
                    if delta_type == "text_delta":
                        value = _text(_raw_get(delta, "text"))
                        self._check_stream_bound(
                            content_chars + reasoning_chars,
                            value,
                            self._MAX_STREAM_TEXT_CHARS,
                            "text/reasoning",
                        )
                        content_chars += len(value)
                        content_parts.append(value)
                        self._emit(
                            on_event,
                            LLMStreamEvent(
                                stage="answer",
                                content_delta=value,
                                content_chars=content_chars,
                                reasoning_chars=reasoning_chars,
                                tool_call_count=len(tool_parts),
                            ),
                        )
                    elif delta_type == "thinking_delta":
                        value = _text(_raw_get(delta, "thinking"))
                        self._check_stream_bound(
                            content_chars + reasoning_chars,
                            value,
                            self._MAX_STREAM_TEXT_CHARS,
                            "text/reasoning",
                        )
                        reasoning_chars += len(value)
                        reasoning_parts.append(value)
                        self._emit(
                            on_event,
                            LLMStreamEvent(
                                stage="reasoning",
                                reasoning_delta=value,
                                content_chars=content_chars,
                                reasoning_chars=reasoning_chars,
                                tool_call_count=len(tool_parts),
                            ),
                        )
                    elif delta_type == "input_json_delta":
                        value = _text(_raw_get(delta, "partial_json"))
                        if (
                            index not in tool_parts
                            and len(tool_parts) >= self._MAX_STREAM_TOOL_CALLS
                        ):
                            raise ValueError(
                                "Anthropic streamed tool calls exceeded the safe accumulation limit"
                            )
                        part = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        self._check_stream_bound(
                            len(part["arguments"]),
                            value,
                            self._MAX_STREAM_TOOL_ARGUMENT_CHARS,
                            "tool arguments",
                        )
                        tool_argument_chars = self._check_stream_bound(
                            tool_argument_chars,
                            value,
                            self._MAX_STREAM_TOTAL_TOOL_ARGUMENT_CHARS,
                            "total tool arguments",
                        )
                        part["arguments"] += value
                        self._emit(
                            on_event,
                            LLMStreamEvent(
                                stage="tool_call",
                                tool_call_id=part["id"] or None,
                                tool_call_name=part["name"] or None,
                                tool_call_arguments_delta=value,
                                content_chars=content_chars,
                                reasoning_chars=reasoning_chars,
                                tool_call_count=len(tool_parts),
                            ),
                        )
                    continue
                if event_type == "message_delta":
                    delta = _raw_get(event, "delta")
                    finish_reason = _text(_raw_get(delta, "stop_reason")) or finish_reason
                    delta_usage = _raw_get(event, "usage")
                    if delta_usage:
                        output_tokens = _raw_int(delta_usage, "output_tokens")
                        usage = usage.model_copy(
                            update={
                                "output_tokens": output_tokens,
                                "total_tokens": usage.input_tokens + output_tokens,
                            }
                        )

        allowed_names = self._allowed_tool_names(tools)
        tool_calls: list[ToolCall] = []
        for index in sorted(tool_parts):
            part = tool_parts[index]
            if part["name"] not in allowed_names:
                logger.warning(
                    "Rejected streamed Anthropic tool call outside offered schema: %s",
                    part["name"],
                )
                log_event(
                    "native_tool_call_rejected",
                    get_run_id() or "unknown",
                    tool=part["name"],
                    reason="not_in_offered_schema",
                )
                continue
            raw_arguments = part["arguments"] or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Malformed streamed tool arguments for {part['name']}") from exc
            if not isinstance(arguments, dict):
                raise ValueError(f"Tool arguments for {part['name']} must be an object")
            tool_calls.append(
                ToolCall(
                    id=part["id"] or f"toolu_{index}",
                    name=part["name"],
                    arguments=arguments,
                )
            )

        response = LLMResponse(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            usage=usage,
        )
        self._capture("chat_streamed", kwargs, response, finish_reason=finish_reason)
        self._emit(
            on_event,
            LLMStreamEvent(
                stage="finished",
                finish_reason=finish_reason,
                content_chars=len(response.content),
                reasoning_chars=len(response.reasoning),
                tool_call_count=len(response.tool_calls),
            ),
        )
        return response

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Convenience stream; orchestration should use :meth:`chat_streamed`."""
        kwargs = self._build_request(messages, tools, system)
        async with self._client.messages.stream(**kwargs) as stream:
            text_stream = getattr(stream, "text_stream", None)
            if text_stream is not None:
                async for value in text_stream:
                    yield StreamChunk(content=value)
                return
            async for event in stream:
                if _raw_get(event, "type") != "content_block_delta":
                    continue
                delta = _raw_get(event, "delta")
                delta_type = _raw_get(delta, "type")
                if delta_type == "text_delta":
                    yield StreamChunk(content=_text(_raw_get(delta, "text")))
                elif delta_type == "thinking_delta":
                    yield StreamChunk(reasoning=_text(_raw_get(delta, "thinking")))
                elif delta_type == "input_json_delta":
                    yield StreamChunk(
                        tool_call_arguments_delta=_text(_raw_get(delta, "partial_json"))
                    )
