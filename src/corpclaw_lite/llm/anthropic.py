# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic
import httpx

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

_ANTHROPIC_METADATA_KEY = "anthropic"
_OPAQUE_THINKING_KEY = "opaque_thinking"


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
    _MAX_STREAM_TOOL_ID_CHARS = 512
    _MAX_STREAM_TOOL_NAME_CHARS = 64
    _MAX_OPAQUE_THINKING_CHARS = 1_000_000
    _MAX_OPAQUE_THINKING_BLOCKS = 128
    _MAX_OPAQUE_THINKING_ENTRIES = 256

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

        # S1-02: explicit transport timeout + retry. Defaults mirror the
        # Anthropic SDK exactly (connect 5s, read/write/pool 600s,
        # max_retries 2) so existing deployments are unaffected. Operators can
        # still tune all four via env.
        timeout = httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.write_timeout,
            pool=settings.pool_timeout,
        )
        client_kwargs: dict[str, Any] = {
            "api_key": settings.api_key,
            "timeout": timeout,
            "max_retries": settings.max_retries,
        }
        if settings.base_url:
            client_kwargs["base_url"] = settings.base_url
        self._client = anthropic.AsyncAnthropic(**client_kwargs)
        # Anthropic requires signed/redacted thinking blocks to be returned
        # unchanged with the tool results.  The generic LLMResponse intentionally
        # exposes only display-safe reasoning text, so keep the opaque protocol
        # blocks provider-local and scope them to the current run/session plus the
        # exact ordered tool-call ids.  The bounded LRU prevents cross-run growth.
        self._opaque_thinking: OrderedDict[
            tuple[tuple[str | None, str | None, int | None], tuple[str, ...]],
            list[dict[str, str]],
        ] = OrderedDict()

    async def aclose(self) -> None:
        """Close the underlying ``AsyncAnthropic`` HTTP client (S1-01).

        Idempotent: the SDK client tolerates repeated close. Required so
        transient provider instances (override-routers, calibration, tests) do
        not leak connection pools / keepalive tasks.
        """
        await self._client.close()

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
    def _tool_call_fields(
        tool_call: Any,
    ) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
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
        raw_metadata = _raw_get(tool_call, "_provider_metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else None
        return call_id, name, arguments, metadata

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate canonical OpenAI history into Anthropic content blocks.

        Consecutive user messages (including tool results) are deliberately
        coalesced because Anthropic requires alternating user/assistant turns.
        """
        converted: list[dict[str, Any]] = []
        pending_tool_ids: set[str] = set()
        for message in messages:
            role = message.get("role")
            if role == "system":
                raise ValueError("System messages must be passed via the system argument")
            if role == "user":
                if pending_tool_ids:
                    raise ValueError("Tool results must immediately follow assistant tool calls")
                self._append_message(
                    converted, "user", self._content_blocks(message.get("content"))
                )
                continue
            if role == "assistant":
                if pending_tool_ids:
                    raise ValueError("Tool results must immediately follow assistant tool calls")
                blocks: list[dict[str, Any]] = self._content_blocks(message.get("content"))
                parsed_calls = [
                    self._tool_call_fields(tool_call)
                    for tool_call in (message.get("tool_calls") or [])
                ]
                call_ids = tuple(call_id for call_id, _name, _arguments, _metadata in parsed_calls)
                if len(set(call_ids)) != len(call_ids):
                    raise ValueError("Assistant tool call ids must be unique")
                opaque_blocks: list[dict[str, str]] = []
                for _call_id, _name, _arguments, metadata in parsed_calls:
                    carried = self._opaque_from_metadata(metadata)
                    if carried:
                        if opaque_blocks and carried != opaque_blocks:
                            raise ValueError("Conflicting Anthropic opaque thinking metadata")
                        opaque_blocks = carried
                if not opaque_blocks:
                    opaque_blocks = self._get_opaque_thinking(call_ids)
                if opaque_blocks:
                    blocks = [dict(block) for block in opaque_blocks] + blocks
                for call_id, name, arguments, _metadata in parsed_calls:
                    blocks.append(
                        {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
                    )
                pending_tool_ids = set(call_ids)
                self._append_message(converted, "assistant", blocks)
                continue
            if role == "tool":
                tool_use_id = message.get("tool_call_id")
                if not isinstance(tool_use_id, str) or not tool_use_id:
                    raise ValueError("Tool result requires a non-empty tool_call_id")
                if tool_use_id not in pending_tool_ids:
                    raise ValueError(
                        f"Tool result {tool_use_id!r} has no pending assistant tool call"
                    )
                content = message.get("content", "")
                result_text = content if isinstance(content, str) else json.dumps(content)
                block = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_text,
                    "is_error": self._is_tool_error(result_text),
                }
                self._append_message(converted, "user", [block])
                pending_tool_ids.remove(tool_use_id)
                continue
            raise ValueError(f"Unsupported message role for Anthropic: {role!r}")
        if pending_tool_ids:
            raise ValueError("Assistant tool calls are missing immediate tool results")
        return converted

    @staticmethod
    def _is_tool_error(result: str) -> bool:
        first_line = result.lstrip().splitlines()[0].lower() if result.strip() else ""
        return bool(re.match(r"^(?:error\b|subagent error\b|mcp tool .+ error:)", first_line))

    @staticmethod
    def _opaque_scope() -> tuple[str | None, str | None, int | None]:
        return get_run_id(), get_capture_user_id(), get_capture_session_id()

    def _opaque_key(
        self, call_ids: tuple[str, ...]
    ) -> tuple[tuple[str | None, str | None, int | None], tuple[str, ...]]:
        return self._opaque_scope(), call_ids

    def _get_opaque_thinking(self, call_ids: tuple[str, ...]) -> list[dict[str, str]]:
        if not call_ids:
            return []
        key = self._opaque_key(call_ids)
        blocks = self._opaque_thinking.get(key)
        if blocks is None:
            return []
        self._opaque_thinking.move_to_end(key)
        return blocks

    def _remember_opaque_thinking(
        self, call_ids: tuple[str, ...], blocks: list[dict[str, str]]
    ) -> None:
        if not call_ids or not blocks:
            return
        self._validate_opaque_collection(blocks)
        key = self._opaque_key(call_ids)
        self._opaque_thinking[key] = [dict(block) for block in blocks]
        self._opaque_thinking.move_to_end(key)
        while len(self._opaque_thinking) > self._MAX_OPAQUE_THINKING_ENTRIES:
            self._opaque_thinking.popitem(last=False)

    def _opaque_block(self, block: Any) -> dict[str, str] | None:
        block_type = _raw_get(block, "type")
        if block_type == "thinking":
            thinking = _text(_raw_get(block, "thinking"))
            signature = _text(_raw_get(block, "signature"))
            if not signature:
                raise ValueError("Anthropic thinking block requires a signature")
            self._check_stream_bound(
                len(thinking), signature, self._MAX_OPAQUE_THINKING_CHARS, "opaque thinking"
            )
            return {"type": "thinking", "thinking": thinking, "signature": signature}
        if block_type == "redacted_thinking":
            data = _text(_raw_get(block, "data"))
            if not data:
                raise ValueError("Anthropic redacted thinking block requires data")
            self._check_stream_bound(0, data, self._MAX_OPAQUE_THINKING_CHARS, "opaque thinking")
            return {"type": "redacted_thinking", "data": data}
        return None

    def _validate_opaque_collection(self, blocks: list[dict[str, str]]) -> None:
        if len(blocks) > self._MAX_OPAQUE_THINKING_BLOCKS:
            raise ValueError("Anthropic opaque thinking block count exceeded the safe limit")
        total_chars = sum(
            len(value) for block in blocks for key, value in block.items() if key != "type"
        )
        if total_chars > self._MAX_OPAQUE_THINKING_CHARS:
            raise ValueError("Anthropic opaque thinking exceeded the safe total accumulation limit")

    def _opaque_from_metadata(self, metadata: dict[str, Any] | None) -> list[dict[str, str]]:
        if not metadata or _ANTHROPIC_METADATA_KEY not in metadata:
            return []
        anthropic_metadata = metadata[_ANTHROPIC_METADATA_KEY]
        if not isinstance(anthropic_metadata, dict) or set(anthropic_metadata) != {
            _OPAQUE_THINKING_KEY
        }:
            raise ValueError("Malformed Anthropic provider metadata")
        raw_blocks = anthropic_metadata[_OPAQUE_THINKING_KEY]
        if not isinstance(raw_blocks, list):
            raise ValueError("Anthropic opaque thinking metadata must be a list")
        blocks: list[dict[str, str]] = []
        for raw_block in raw_blocks:
            if not isinstance(raw_block, dict):
                raise ValueError("Malformed Anthropic opaque thinking block")
            opaque = self._opaque_block(raw_block)
            if opaque is None:
                raise ValueError("Unsupported Anthropic opaque thinking block")
            if set(raw_block) != set(opaque):
                raise ValueError("Unsupported keys in Anthropic opaque thinking block")
            blocks.append(opaque)
        self._validate_opaque_collection(blocks)
        return blocks

    @staticmethod
    def _metadata_for_opaque(blocks: list[dict[str, str]]) -> dict[str, Any] | None:
        if not blocks:
            return None
        return {
            _ANTHROPIC_METADATA_KEY: {
                _OPAQUE_THINKING_KEY: [dict(block) for block in blocks],
            }
        }

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
            # Manual extended thinking is incompatible with modified sampling
            # controls.  Let Anthropic use its required defaults instead of
            # sending a request the API will reject.
            for incompatible in ("temperature", "top_p", "top_k"):
                if incompatible in kwargs:
                    logger.warning(
                        "Ignoring Anthropic %s while extended thinking is enabled",
                        incompatible,
                    )
                    kwargs.pop(incompatible, None)
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
    def _capture_safe_messages(messages: Any) -> Any:
        """Remove opaque replay credentials from opt-in payload diagnostics."""

        if not isinstance(messages, list):
            return messages
        safe_messages: list[Any] = []
        for message in messages:
            if not isinstance(message, dict):
                safe_messages.append(message)
                continue
            safe_message = dict(message)
            content = safe_message.get("content")
            if isinstance(content, list):
                safe_blocks: list[Any] = []
                for raw_block in content:
                    if not isinstance(raw_block, dict):
                        safe_blocks.append(raw_block)
                        continue
                    block = dict(raw_block)
                    if block.get("type") == "thinking":
                        block.pop("signature", None)
                    elif block.get("type") == "redacted_thinking":
                        block["data"] = "[OPAQUE_REDACTED_THINKING]"
                    safe_blocks.append(block)
                safe_message["content"] = safe_blocks
            safe_messages.append(safe_message)
        return safe_messages

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
        opaque_thinking: list[dict[str, str]] = []
        allowed_names = self._allowed_tool_names(tools)
        for block in _raw_get(response, "content", []) or []:
            block_type = _raw_get(block, "type")
            if block_type == "text":
                content_parts.append(_text(_raw_get(block, "text")))
            elif block_type == "thinking":
                reasoning_parts.append(_text(_raw_get(block, "thinking")))
                opaque = self._opaque_block(block)
                if opaque is not None:
                    opaque_thinking.append(opaque)
                    self._validate_opaque_collection(opaque_thinking)
            elif block_type == "redacted_thinking":
                opaque = self._opaque_block(block)
                if opaque is not None:
                    opaque_thinking.append(opaque)
                    self._validate_opaque_collection(opaque_thinking)
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
                call_id = str(_raw_get(block, "id", ""))
                if not call_id:
                    raise ValueError("Anthropic tool call requires a non-empty id")
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
        if tool_calls and opaque_thinking:
            tool_calls[0] = tool_calls[0].model_copy(
                update={"provider_metadata": self._metadata_for_opaque(opaque_thinking)}
            )
        self._remember_opaque_thinking(tuple(call.id for call in tool_calls), opaque_thinking)
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
                "messages": self._capture_safe_messages(kwargs.get("messages")),
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
        opaque_parts: dict[int, dict[str, str]] = {}
        content_chars = 0
        reasoning_chars = 0
        tool_argument_chars = 0
        opaque_chars = 0
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
                    block_type = _raw_get(block, "type")
                    if block_type == "tool_use":
                        if (
                            index not in tool_parts
                            and len(tool_parts) >= self._MAX_STREAM_TOOL_CALLS
                        ):
                            raise ValueError(
                                "Anthropic streamed tool calls exceeded the safe accumulation limit"
                            )
                        call_id = str(_raw_get(block, "id", ""))
                        name = str(_raw_get(block, "name", ""))
                        self._check_stream_bound(
                            0, call_id, self._MAX_STREAM_TOOL_ID_CHARS, "tool call id"
                        )
                        self._check_stream_bound(
                            0, name, self._MAX_STREAM_TOOL_NAME_CHARS, "tool call name"
                        )
                        tool_parts[index] = {
                            "id": call_id,
                            "name": name,
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
                    elif block_type == "thinking":
                        if (
                            index not in opaque_parts
                            and len(opaque_parts) >= self._MAX_OPAQUE_THINKING_BLOCKS
                        ):
                            raise ValueError(
                                "Anthropic opaque thinking block count exceeded the safe limit"
                            )
                        thinking = _text(_raw_get(block, "thinking"))
                        signature = _text(_raw_get(block, "signature"))
                        self._check_stream_bound(
                            len(thinking),
                            signature,
                            self._MAX_OPAQUE_THINKING_CHARS,
                            "opaque thinking",
                        )
                        opaque_chars = self._check_stream_bound(
                            opaque_chars,
                            thinking + signature,
                            self._MAX_OPAQUE_THINKING_CHARS,
                            "total opaque thinking",
                        )
                        opaque_parts[index] = {
                            "type": "thinking",
                            "thinking": thinking,
                            "signature": signature,
                        }
                    elif block_type == "redacted_thinking":
                        if (
                            index not in opaque_parts
                            and len(opaque_parts) >= self._MAX_OPAQUE_THINKING_BLOCKS
                        ):
                            raise ValueError(
                                "Anthropic opaque thinking block count exceeded the safe limit"
                            )
                        data = _text(_raw_get(block, "data"))
                        self._check_stream_bound(
                            0,
                            data,
                            self._MAX_OPAQUE_THINKING_CHARS,
                            "opaque thinking",
                        )
                        opaque_chars = self._check_stream_bound(
                            opaque_chars,
                            data,
                            self._MAX_OPAQUE_THINKING_CHARS,
                            "total opaque thinking",
                        )
                        opaque_parts[index] = {"type": "redacted_thinking", "data": data}
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
                        if (
                            index not in opaque_parts
                            and len(opaque_parts) >= self._MAX_OPAQUE_THINKING_BLOCKS
                        ):
                            raise ValueError(
                                "Anthropic opaque thinking block count exceeded the safe limit"
                            )
                        opaque = opaque_parts.setdefault(
                            index,
                            {"type": "thinking", "thinking": "", "signature": ""},
                        )
                        self._check_stream_bound(
                            len(opaque.get("thinking", "")) + len(opaque.get("signature", "")),
                            value,
                            self._MAX_OPAQUE_THINKING_CHARS,
                            "opaque thinking",
                        )
                        opaque_chars = self._check_stream_bound(
                            opaque_chars,
                            value,
                            self._MAX_OPAQUE_THINKING_CHARS,
                            "total opaque thinking",
                        )
                        opaque["thinking"] = opaque.get("thinking", "") + value
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
                    elif delta_type == "signature_delta":
                        value = _text(_raw_get(delta, "signature"))
                        if (
                            index not in opaque_parts
                            and len(opaque_parts) >= self._MAX_OPAQUE_THINKING_BLOCKS
                        ):
                            raise ValueError(
                                "Anthropic opaque thinking block count exceeded the safe limit"
                            )
                        opaque = opaque_parts.setdefault(
                            index,
                            {"type": "thinking", "thinking": "", "signature": ""},
                        )
                        self._check_stream_bound(
                            len(opaque.get("thinking", "")) + len(opaque.get("signature", "")),
                            value,
                            self._MAX_OPAQUE_THINKING_CHARS,
                            "opaque thinking",
                        )
                        opaque_chars = self._check_stream_bound(
                            opaque_chars,
                            value,
                            self._MAX_OPAQUE_THINKING_CHARS,
                            "total opaque thinking",
                        )
                        opaque["signature"] = opaque.get("signature", "") + value
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
                # S2-01: degrade instead of crashing the run. The non-streamed
                # path (_parse_response) already skips malformed tool calls with
                # a warning; the streamed path must match so a single malformed
                # streamed tool call does not crash the main-agent run
                # (llm_streaming_enabled is the default main-agent path).
                logger.warning(
                    "Rejected streamed Anthropic tool call with malformed arguments: %s (%s)",
                    part["name"],
                    exc,
                )
                log_event(
                    "native_tool_call_rejected",
                    get_run_id() or "unknown",
                    tool=part["name"],
                    reason="malformed_streamed_arguments",
                )
                continue
            if not isinstance(arguments, dict):
                logger.warning(
                    "Rejected streamed Anthropic tool call with non-object arguments: %s",
                    part["name"],
                )
                log_event(
                    "native_tool_call_rejected",
                    get_run_id() or "unknown",
                    tool=part["name"],
                    reason="non_object_streamed_arguments",
                )
                continue
            if not part["id"]:
                logger.warning(
                    "Rejected streamed Anthropic tool call without an id: %s",
                    part["name"],
                )
                log_event(
                    "native_tool_call_rejected",
                    get_run_id() or "unknown",
                    tool=part["name"],
                    reason="missing_streamed_id",
                )
                continue
            tool_calls.append(
                ToolCall(
                    id=part["id"],
                    name=part["name"],
                    arguments=arguments,
                )
            )

        opaque_thinking: list[dict[str, str]] = []
        for index in sorted(opaque_parts):
            opaque = self._opaque_block(opaque_parts[index])
            if opaque is not None:
                opaque_thinking.append(opaque)
        self._validate_opaque_collection(opaque_thinking)
        if tool_calls and opaque_thinking:
            tool_calls[0] = tool_calls[0].model_copy(
                update={"provider_metadata": self._metadata_for_opaque(opaque_thinking)}
            )
        self._remember_opaque_thinking(tuple(call.id for call in tool_calls), opaque_thinking)

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
