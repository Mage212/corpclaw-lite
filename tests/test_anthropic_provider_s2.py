from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.config.providers import ProviderSettings
from corpclaw_lite.llm.anthropic import AnthropicProvider
from corpclaw_lite.llm.base import (
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
            SimpleNamespace(type="thinking", thinking="private thought"),
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
            "content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="think "),
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
