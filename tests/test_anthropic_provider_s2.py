from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.agent.context import ContextBuilder
from corpclaw_lite.config.providers import ProviderSettings
from corpclaw_lite.llm.anthropic import AnthropicProvider
from corpclaw_lite.llm.base import (
    LLMResponse,
    LLMStreamEvent,
    RequestOptions,
    ThinkingOverride,
    reset_capture_context,
    reset_request_options,
    reset_run_id,
    set_capture_context,
    set_request_options,
    set_run_id,
)
from corpclaw_lite.llm.presets import ModelProfile, SamplingProfile
from corpclaw_lite.users.models import User


def _settings() -> ProviderSettings:
    return ProviderSettings(type="anthropic", model="claude-test", api_key="sk-ant-test")


def _tools(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Call {name}",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _text_response(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        stop_reason="end_turn",
    )


def _provider(client: Any, **kwargs: Any) -> AnthropicProvider:
    with patch("corpclaw_lite.llm.anthropic.anthropic.AsyncAnthropic", return_value=client):
        return AnthropicProvider(_settings(), **kwargs)


@pytest.mark.asyncio
async def test_openai_tool_history_converts_and_batches_results() -> None:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_text_response())
    provider = _provider(client)
    history = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": "working",
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": {}},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "alpha"},
        {"role": "tool", "tool_call_id": "call_b", "content": "Error: denied"},
        {"role": "user", "content": "continue"},
    ]

    await provider.chat(history, tools=_tools("read_file", "list_files"))

    sent = client.messages.create.await_args.kwargs["messages"]
    assert [message["role"] for message in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == [
        {"type": "text", "text": "working"},
        {
            "type": "tool_use",
            "id": "call_a",
            "name": "read_file",
            "input": {"path": "a"},
        },
        {
            "type": "tool_use",
            "id": "call_b",
            "name": "list_files",
            "input": {},
        },
    ]
    assert [block["type"] for block in sent[2]["content"]] == [
        "tool_result",
        "tool_result",
        "text",
    ]
    assert sent[2]["content"][0]["is_error"] is False
    assert sent[2]["content"][1]["is_error"] is True


@pytest.mark.asyncio
async def test_full_tool_use_tool_result_roundtrip() -> None:
    first_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use", id="toolu_roundtrip", name="read_file", input={"path": "a"}
            )
        ],
        usage=SimpleNamespace(input_tokens=4, output_tokens=3),
        stop_reason="tool_use",
    )
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=[first_response, _text_response("done")])
    provider = _provider(client)
    offered = _tools("read_file")

    first = await provider.chat([{"role": "user", "content": "read a"}], tools=offered)
    call = first.tool_calls[0]
    history = [
        {"role": "user", "content": "read a"},
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call.id, "content": "file contents"},
    ]
    second = await provider.chat(history, tools=offered)

    assert second.content == "done"
    sent = client.messages.create.await_args_list[1].kwargs["messages"]
    assert sent[1]["content"][0]["type"] == "tool_use"
    assert sent[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_roundtrip",
        "content": "file contents",
        "is_error": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages, match",
    [
        ([{"role": "system", "content": "unsafe"}], "System messages"),
        (
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "function": {"name": "read_file", "arguments": "{bad"},
                        }
                    ],
                }
            ],
            "Malformed tool arguments",
        ),
    ],
)
async def test_invalid_history_rejected_before_network(
    messages: list[dict[str, Any]], match: str
) -> None:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_text_response())
    provider = _provider(client)

    with pytest.raises(ValueError, match=match):
        await provider.chat(messages)

    client.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_parses_reasoning_and_filters_unoffered_tool() -> None:
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="private thought", signature="signed"),
            SimpleNamespace(type="text", text="answer"),
            SimpleNamespace(type="tool_use", id="bad", name="exec_script", input={}),
            SimpleNamespace(type="tool_use", id="good", name="read_file", input={"path": "a"}),
        ],
        usage=SimpleNamespace(input_tokens=5, output_tokens=7),
        stop_reason="tool_use",
    )
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    provider = _provider(client)

    result = await provider.chat([], tools=_tools("read_file"))

    assert result.content == "answer"
    assert result.reasoning == "private thought"
    assert [call.name for call in result.tool_calls] == ["read_file"]


@pytest.mark.asyncio
async def test_request_priority_and_thinking_override() -> None:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_text_response())
    provider = _provider(
        client,
        model_profile=ModelProfile(
            system_prompt_prefix="MODEL PREFIX",
            default_inference={"temperature": 0.8, "top_p": 0.7},
        ),
        sampling=SamplingProfile(
            thinking_mode="off",
            inference_overrides={"temperature": 0.4},
        ),
    )
    token = set_request_options(
        RequestOptions(
            inference={"temperature": 0.2, "unsupported": "ignored"},
            thinking=ThinkingOverride(mode="default"),
        )
    )
    try:
        await provider.chat([{"role": "user", "content": "hi"}], system="BASE")
    finally:
        reset_request_options(token)

    kwargs = client.messages.create.await_args.kwargs
    assert kwargs["temperature"] == 0.2
    assert kwargs["top_p"] == 0.7
    assert kwargs["system"] == "MODEL PREFIX\nBASE"
    assert "thinking" not in kwargs
    assert "unsupported" not in kwargs


@pytest.mark.asyncio
async def test_request_thinking_budget_is_validated() -> None:
    client = MagicMock()
    provider = _provider(
        client, sampling=SamplingProfile(thinking_mode="budget", thinking_budget=2048)
    )
    token = set_request_options(RequestOptions(inference={"max_tokens": 2048}))
    try:
        with pytest.raises(ValueError, match="must exceed"):
            await provider.chat([])
    finally:
        reset_request_options(token)


class _EventStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            yield event


class _StreamManager:
    def __init__(self, events: list[Any]) -> None:
        self._stream = _EventStream(events)

    async def __aenter__(self) -> _EventStream:
        return self._stream

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


def _event(type_: str, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **kwargs)


@pytest.mark.asyncio
async def test_chat_streamed_reconstructs_reasoning_text_tool_and_usage() -> None:
    events = [
        _event(
            "message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=11, output_tokens=0)),
        ),
        _event(
            "content_block_start",
            index=0,
            content_block=SimpleNamespace(type="thinking", thinking="", signature=""),
        ),
        _event(
            "content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="think "),
        ),
        _event(
            "content_block_delta",
            index=0,
            delta=SimpleNamespace(type="signature_delta", signature="stream-signature"),
        ),
        _event(
            "content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="answer"),
        ),
        _event(
            "content_block_start",
            index=2,
            content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="read_file"),
        ),
        _event(
            "content_block_delta",
            index=2,
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"path":'),
        ),
        _event(
            "content_block_delta",
            index=2,
            delta=SimpleNamespace(type="input_json_delta", partial_json='"a"}'),
        ),
        _event(
            "message_delta",
            delta=SimpleNamespace(stop_reason="tool_use"),
            usage=SimpleNamespace(output_tokens=9),
        ),
    ]
    client = MagicMock()
    client.messages.stream = MagicMock(return_value=_StreamManager(events))
    provider = _provider(client)
    emitted: list[LLMStreamEvent] = []

    result = await provider.chat_streamed(
        [{"role": "user", "content": "inspect"}],
        tools=_tools("read_file"),
        on_event=emitted.append,
    )

    assert result.content == "answer"
    assert result.reasoning == "think "
    assert result.tool_calls[0].arguments == {"path": "a"}
    assert result.usage.total_tokens == 20
    assert [event.stage for event in emitted] == [
        "started",
        "reasoning",
        "answer",
        "tool_call",
        "tool_call",
        "tool_call",
        "finished",
    ]
    converted = provider._convert_messages(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "contents"},
        ]
    )
    assert converted[0]["content"][0] == {
        "type": "thinking",
        "thinking": "think ",
        "signature": "stream-signature",
    }


@pytest.mark.asyncio
async def test_chat_streamed_argument_limit_fails_before_tool_call() -> None:
    events = [
        _event(
            "content_block_start",
            index=0,
            content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="read_file"),
        ),
        _event(
            "content_block_delta",
            index=0,
            delta=SimpleNamespace(type="input_json_delta", partial_json="12345"),
        ),
    ]
    client = MagicMock()
    client.messages.stream = MagicMock(return_value=_StreamManager(events))
    provider = _provider(client)
    provider._MAX_STREAM_TOOL_ARGUMENT_CHARS = 4

    with pytest.raises(ValueError, match="safe accumulation limit"):
        await provider.chat_streamed([], tools=_tools("read_file"))


@pytest.mark.asyncio
async def test_chat_streamed_total_argument_limit_bounds_many_calls() -> None:
    events: list[Any] = []
    for index in range(2):
        events.extend(
            [
                _event(
                    "content_block_start",
                    index=index,
                    content_block=SimpleNamespace(
                        type="tool_use", id=f"toolu_{index}", name="read_file"
                    ),
                ),
                _event(
                    "content_block_delta",
                    index=index,
                    delta=SimpleNamespace(type="input_json_delta", partial_json="123"),
                ),
            ]
        )
    client = MagicMock()
    client.messages.stream = MagicMock(return_value=_StreamManager(events))
    provider = _provider(client)
    provider._MAX_STREAM_TOTAL_TOOL_ARGUMENT_CHARS = 5

    with pytest.raises(ValueError, match="safe accumulation limit"):
        await provider.chat_streamed([], tools=_tools("read_file"))


@pytest.mark.asyncio
async def test_payload_capture_keeps_run_user_and_session_correlation() -> None:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_text_response())
    provider = _provider(client)
    payload_logger = MagicMock(enabled=True)
    run_token = set_run_id("run-7")
    capture_tokens = set_capture_context("user-2", 42)
    try:
        with patch("corpclaw_lite.logging.payload.get_payload_logger", return_value=payload_logger):
            await provider.chat([{"role": "user", "content": "hello"}])
    finally:
        reset_capture_context(capture_tokens)
        reset_run_id(run_token)

    captured = payload_logger.capture.call_args.kwargs
    assert captured["run_id"] == "run-7"
    assert captured["user_id"] == "user-2"
    assert captured["session_id"] == 42


def test_payload_capture_removes_opaque_thinking_credentials() -> None:
    client = MagicMock()
    provider = _provider(client)
    payload_logger = MagicMock(enabled=True)
    kwargs = {
        "model": "claude-test",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "display-safe summary",
                        "signature": "secret-signature",
                    },
                    {"type": "redacted_thinking", "data": "secret-redacted-data"},
                ],
            }
        ],
    }

    with patch("corpclaw_lite.logging.payload.get_payload_logger", return_value=payload_logger):
        provider._capture("chat", kwargs, LLMResponse(content=""), finish_reason="tool_use")

    captured_request = payload_logger.capture.call_args.kwargs["request"]
    serialized = json.dumps(captured_request)
    assert "secret-signature" not in serialized
    assert "secret-redacted-data" not in serialized
    assert "display-safe summary" in serialized


@pytest.mark.asyncio
async def test_signed_and_redacted_thinking_round_trip_opaquely_with_tool_result() -> None:
    first_raw = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="private", signature="sig-1"),
            SimpleNamespace(type="redacted_thinking", data="opaque-redacted"),
            SimpleNamespace(type="tool_use", id="toolu_signed", name="read_file", input={}),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
        stop_reason="tool_use",
    )
    first_client = MagicMock()
    first_client.messages.create = AsyncMock(return_value=first_raw)
    first_provider = _provider(
        first_client,
        sampling=SamplingProfile(thinking_mode="budget", thinking_budget=1024),
    )
    offered = _tools("read_file")

    first = await first_provider.chat([{"role": "user", "content": "read"}], tools=offered)
    assert first.reasoning == "private"
    assert "sig-1" not in first.reasoning
    assert "opaque-redacted" not in first.content
    assert first.model_dump()["tool_calls"] == [
        {"id": "toolu_signed", "name": "read_file", "arguments": {}}
    ]

    transcript = ContextBuilder()
    transcript.add_user_message("read")
    transcript.add_tool_calls(first.tool_calls)
    transcript.add_tool_result("toolu_signed", "read_file", "contents")
    durable_messages = json.loads(json.dumps(transcript.messages))
    restored = ContextBuilder.build_from_full_history(
        User(id=1, name="Restart", department="engineering"),
        "continue",
        durable_messages,
    )

    # A new provider instance simulates process restart: no in-memory LRU entry
    # exists, so the signed blocks can only come from canonical transcript data.
    second_client = MagicMock()
    second_client.messages.create = AsyncMock(return_value=_text_response("done"))
    restarted_provider = _provider(
        second_client,
        sampling=SamplingProfile(thinking_mode="budget", thinking_budget=1024),
    )
    await restarted_provider.chat(restored.messages, tools=offered)

    assistant_blocks = second_client.messages.create.await_args.kwargs["messages"][1]["content"]
    assert assistant_blocks[:2] == [
        {"type": "thinking", "thinking": "private", "signature": "sig-1"},
        {"type": "redacted_thinking", "data": "opaque-redacted"},
    ]
    assert assistant_blocks[2]["type"] == "tool_use"


@pytest.mark.asyncio
async def test_budget_thinking_removes_incompatible_sampling_controls() -> None:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_text_response())
    provider = _provider(
        client,
        model_profile=ModelProfile(default_inference={"temperature": 0.8, "top_p": 0.7}),
        sampling=SamplingProfile(
            thinking_mode="budget",
            thinking_budget=2048,
            inference_overrides={"top_k": 20},
        ),
    )
    token = set_request_options(RequestOptions(inference={"temperature": 0.2}))
    try:
        await provider.chat([{"role": "user", "content": "hard question"}])
    finally:
        reset_request_options(token)

    kwargs = client.messages.create.await_args.kwargs
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert not {"temperature", "top_p", "top_k"} & kwargs.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history",
    [
        [
            {"role": "user", "content": "orphan first"},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "result"},
        ],
        [
            {"role": "user", "content": "call"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "user", "content": "intervening text"},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "result"},
        ],
    ],
)
async def test_malformed_tool_result_order_rejected_before_network(
    history: list[dict[str, Any]],
) -> None:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_text_response())
    provider = _provider(client)

    with pytest.raises(ValueError, match="Tool result"):
        await provider.chat(history, tools=_tools("read_file"))

    client.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_stream_tool_call_with_empty_id_is_rejected() -> None:
    raw = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="", name="read_file", input={})],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="tool_use",
    )
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=raw)
    provider = _provider(client)

    with pytest.raises(ValueError, match="non-empty id"):
        await provider.chat([{"role": "user", "content": "read"}], tools=_tools("read_file"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "block"),
    [
        (
            "tool call id",
            SimpleNamespace(type="tool_use", id="x" * 513, name="read_file"),
        ),
        (
            "tool call name",
            SimpleNamespace(type="tool_use", id="toolu_1", name="x" * 65),
        ),
    ],
)
async def test_streamed_tool_metadata_is_bounded(field: str, block: Any) -> None:
    events = [_event("content_block_start", index=0, content_block=block)]
    client = MagicMock()
    client.messages.stream = MagicMock(return_value=_StreamManager(events))
    provider = _provider(client)

    with pytest.raises(ValueError, match=field):
        await provider.chat_streamed([], tools=_tools("read_file"))


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_kind", ["count", "total"])
async def test_non_stream_opaque_thinking_collection_is_bounded(limit_kind: str) -> None:
    raw = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="a", signature="b"),
            SimpleNamespace(type="redacted_thinking", data="xxxx"),
            SimpleNamespace(type="tool_use", id="toolu_1", name="read_file", input={}),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="tool_use",
    )
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=raw)
    provider = _provider(client)
    if limit_kind == "count":
        provider._MAX_OPAQUE_THINKING_BLOCKS = 1
        match = "block count"
    else:
        provider._MAX_OPAQUE_THINKING_CHARS = 5
        match = "total accumulation"

    with pytest.raises(ValueError, match=match):
        await provider.chat([{"role": "user", "content": "read"}], tools=_tools("read_file"))


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_kind", ["count", "total"])
async def test_streamed_opaque_thinking_collection_is_bounded(limit_kind: str) -> None:
    events = [
        _event(
            "content_block_start",
            index=0,
            content_block=SimpleNamespace(type="redacted_thinking", data="abc"),
        ),
        _event(
            "content_block_start",
            index=1,
            content_block=SimpleNamespace(type="redacted_thinking", data="abc"),
        ),
    ]
    client = MagicMock()
    client.messages.stream = MagicMock(return_value=_StreamManager(events))
    provider = _provider(client)
    if limit_kind == "count":
        provider._MAX_OPAQUE_THINKING_BLOCKS = 1
        match = "block count"
    else:
        provider._MAX_OPAQUE_THINKING_CHARS = 5
        match = "total opaque thinking"

    with pytest.raises(ValueError, match=match):
        await provider.chat_streamed([], tools=_tools("read_file"))


@pytest.mark.parametrize(
    "result",
    [
        "Error: denied",
        "Error executing 'read_file': RuntimeError: boom",
        "Subagent error: timed out",
        "MCP tool 'echo' error: connection lost",
    ],
)
def test_tool_result_error_formats_set_is_error(result: str) -> None:
    client = MagicMock()
    provider = _provider(client)
    converted = provider._convert_messages(
        [
            {"role": "user", "content": "call"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": result},
        ]
    )

    assert converted[-1]["content"][0]["is_error"] is True
