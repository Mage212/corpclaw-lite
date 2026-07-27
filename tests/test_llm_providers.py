"""Tests for LLM providers: AnthropicProvider and OpenAIProvider.

Uses AsyncMock to avoid real API calls. Validates provider wraps SDK correctly,
handles tool calls, system prompts, and edge cases.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.config.providers import ProviderSettings
from corpclaw_lite.llm.base import LLMResponse, ToolCall


def test_xml_fallback_parsing() -> None:
    from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_call

    content = """
Here is my answer.
<tool_call>
<name>read_file</name>
<arguments>{"path": "/tmp/test"}</arguments>
</tool_call>
"""
    result = parse_xml_tool_call(content)
    assert result.status == "valid"
    assert result.tool_call is not None
    assert result.tool_call.name == "read_file"
    assert result.tool_call.arguments["path"] == "/tmp/test"


# ── Helpers ───────────────────────────────────────────────────────────────────────


def _anthropic_settings() -> ProviderSettings:
    return ProviderSettings(
        type="anthropic", model="claude-3-haiku-20240307", api_key="sk-ant-test123"
    )


def _openai_settings(base_url: str = "http://localhost:11434/v1") -> ProviderSettings:
    return ProviderSettings(type="openai", model="qwen2.5:7b", api_key="ollama", base_url=base_url)


# ── AnthropicProvider ─────────────────────────────────────────────────────────────


class TestAnthropicProvider:
    @staticmethod
    def _tools(*names: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in names
        ]

    def _text_response(self, text: str) -> MagicMock:
        block = MagicMock()
        block.type = "text"
        block.text = text
        msg = MagicMock()
        msg.content = [block]
        msg.stop_reason = "end_turn"
        msg.usage.input_tokens = 10
        msg.usage.output_tokens = 20
        return msg

    def _tool_response(self, tool_name: str, tool_input: dict[str, Any]) -> MagicMock:
        block = MagicMock()
        block.type = "tool_use"
        block.id = "toolu_01abc"
        block.name = tool_name
        block.input = tool_input
        msg = MagicMock()
        msg.content = [block]
        msg.stop_reason = "tool_use"
        msg.usage.input_tokens = 15
        msg.usage.output_tokens = 25
        return msg

    @pytest.mark.asyncio
    async def test_text_response(self) -> None:
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=self._text_response("Привет!"))

        with patch("corpclaw_lite.llm.anthropic.anthropic") as mock_mod:
            mock_mod.AsyncAnthropic.return_value = mock_client
            provider = AnthropicProvider(_anthropic_settings())

        result = await provider.chat(messages=[{"role": "user", "content": "Привет"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "Привет!"
        assert result.tool_calls == []
        # usage dict may have input/output tokens
        assert isinstance(result.usage.input_tokens, int)
        assert isinstance(result.usage.output_tokens, int)

    @pytest.mark.asyncio
    async def test_tool_call_response(self) -> None:
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=self._tool_response("read_file", {"path": "test.txt"})
        )

        with patch("corpclaw_lite.llm.anthropic.anthropic") as mock_mod:
            mock_mod.AsyncAnthropic.return_value = mock_client
            provider = AnthropicProvider(_anthropic_settings())

        result = await provider.chat(messages=[], tools=self._tools("read_file"))

        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert isinstance(tc, ToolCall)
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "test.txt"}

    @pytest.mark.asyncio
    async def test_mixed_text_and_tool(self) -> None:
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Читаю файл."

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "toolu_02"
        tool_block.name = "read_file"
        tool_block.input = {"path": "data.csv"}

        msg = MagicMock()
        msg.content = [text_block, tool_block]
        msg.stop_reason = "tool_use"
        msg.usage.input_tokens = 20
        msg.usage.output_tokens = 30

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=msg)

        with patch("corpclaw_lite.llm.anthropic.anthropic") as mock_mod:
            mock_mod.AsyncAnthropic.return_value = mock_client
            provider = AnthropicProvider(_anthropic_settings())

        result = await provider.chat(messages=[], tools=self._tools("read_file"))

        assert result.content == "Читаю файл."
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"

    @pytest.mark.asyncio
    async def test_system_prompt_passed(self) -> None:
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=self._text_response("OK"))

        with patch("corpclaw_lite.llm.anthropic.anthropic") as mock_mod:
            mock_mod.AsyncAnthropic.return_value = mock_client
            provider = AnthropicProvider(_anthropic_settings())

        await provider.chat(
            messages=[{"role": "user", "content": "Hello"}],
            system="You are helpful.",
        )

        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs.get("system") == "You are helpful."


# ── OpenAIProvider ────────────────────────────────────────────────────────────────


class TestOpenAIProvider:
    def _text_resp(self, text: str) -> MagicMock:
        choice = MagicMock()
        choice.message.content = text
        choice.message.tool_calls = None
        choice.finish_reason = "stop"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage.prompt_tokens = 8
        resp.usage.completion_tokens = 16
        resp.usage.total_tokens = 99
        return resp

    def _tool_resp(self, tool_name: str, args: dict[str, Any]) -> MagicMock:
        tc = MagicMock()
        tc.id = "call_abc"
        tc.type = "function"
        tc.function.name = tool_name
        tc.function.arguments = json.dumps(args)
        choice = MagicMock()
        choice.message.content = None
        choice.message.tool_calls = [tc]
        choice.finish_reason = "tool_calls"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage.prompt_tokens = 12
        resp.usage.completion_tokens = 18
        resp.usage.total_tokens = 30
        return resp

    @pytest.mark.asyncio
    async def test_text_response(self) -> None:
        from corpclaw_lite.llm.openai import OpenAIProvider

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=self._text_resp("Привет от локальной LLM!")
        )

        with patch("corpclaw_lite.llm.openai.openai") as mock_mod:
            mock_mod.AsyncOpenAI.return_value = mock_client
            provider = OpenAIProvider(_openai_settings())

        result = await provider.chat(messages=[{"role": "user", "content": "Привет"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "Привет от локальной LLM!"
        assert result.tool_calls == []
        assert isinstance(result.usage.input_tokens, int)
        assert isinstance(result.usage.output_tokens, int)
        assert result.usage.total_tokens == 99

    def test_usage_total_tokens_fallback(self) -> None:
        from corpclaw_lite.llm.openai import _usage_from_raw

        usage = _usage_from_raw(
            {
                "prompt_tokens": 8,
                "completion_tokens": 16,
                "prompt_tokens_details": {"cached_tokens": 3},
            }
        )

        assert usage.input_tokens == 8
        assert usage.output_tokens == 16
        assert usage.total_tokens == 24
        assert usage.cached_input_tokens == 3

    @pytest.mark.asyncio
    async def test_tool_call_response(self) -> None:
        from corpclaw_lite.llm.openai import OpenAIProvider

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=self._tool_resp("list_files", {"path": "/"})
        )

        with patch("corpclaw_lite.llm.openai.openai") as mock_mod:
            mock_mod.AsyncOpenAI.return_value = mock_client
            provider = OpenAIProvider(_openai_settings())

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        result = await provider.chat(messages=[], tools=tools)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "list_files"
        assert result.tool_calls[0].arguments == {"path": "/"}
        assert result.usage.total_tokens == 30

    @pytest.mark.asyncio
    async def test_provider_metadata_is_stripped_from_actual_openai_payload(self) -> None:
        from corpclaw_lite.llm.openai import OpenAIProvider

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=self._text_resp("done"))
        with patch("corpclaw_lite.llm.openai.openai") as mock_mod:
            mock_mod.AsyncOpenAI.return_value = mock_client
            provider = OpenAIProvider(_openai_settings())

        canonical = [
            {"role": "user", "content": "read"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                        "_provider_metadata": {
                            "anthropic": {
                                "opaque_thinking": [
                                    {"type": "thinking", "thinking": "private", "signature": "sig"}
                                ]
                            }
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "contents"},
        ]

        await provider.chat(canonical)

        sent = mock_client.chat.completions.create.await_args.kwargs["messages"]
        assert "_provider_metadata" not in sent[1]["tool_calls"][0]
        assert "provider_metadata" not in sent[1]["tool_calls"][0]
        assert "sig" not in json.dumps(sent)
        # Sanitization must not destroy the durable canonical carrier.
        assert canonical[1]["tool_calls"][0]["_provider_metadata"]["anthropic"]

    @pytest.mark.asyncio
    async def test_native_tool_call_outside_offered_schema_is_rejected(self) -> None:
        from corpclaw_lite.llm.openai import OpenAIProvider

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=self._tool_resp("exec_script", {"script": "id"})
        )
        with patch("corpclaw_lite.llm.openai.openai") as mock_mod:
            mock_mod.AsyncOpenAI.return_value = mock_client
            provider = OpenAIProvider(_openai_settings())

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        result = await provider.chat(messages=[], tools=tools)

        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_empty_tool_calls_list(self) -> None:
        from corpclaw_lite.llm.openai import OpenAIProvider

        choice = MagicMock()
        choice.message.content = "Ок!"
        choice.message.tool_calls = []
        choice.finish_reason = "stop"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage.prompt_tokens = 5
        resp.usage.completion_tokens = 3

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=resp)

        with patch("corpclaw_lite.llm.openai.openai") as mock_mod:
            mock_mod.AsyncOpenAI.return_value = mock_client
            provider = OpenAIProvider(_openai_settings())

        result = await provider.chat(messages=[])

        assert result.content == "Ок!"
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_system_prompt_prepended(self) -> None:
        from corpclaw_lite.llm.openai import OpenAIProvider

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=self._text_resp("OK"))

        with patch("corpclaw_lite.llm.openai.openai") as mock_mod:
            mock_mod.AsyncOpenAI.return_value = mock_client
            provider = OpenAIProvider(_openai_settings())

        await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            system="You are helpful.",
        )

        kwargs = mock_client.chat.completions.create.call_args.kwargs
        sent_messages = kwargs["messages"]
        assert sent_messages[0] == {"role": "system", "content": "You are helpful."}


# ── S1-01: provider aclose ────────────────────────────────────────────────────────


class TestProviderAclose:
    """S1-01: providers must close their underlying SDK HTTP client via aclose()."""

    @pytest.mark.asyncio
    async def test_openai_provider_aclose_closes_client(self) -> None:
        from corpclaw_lite.llm.openai import OpenAIProvider

        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        with patch("corpclaw_lite.llm.openai.openai") as mock_mod:
            mock_mod.AsyncOpenAI.return_value = mock_client
            provider = OpenAIProvider(_openai_settings())

        await provider.aclose()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_anthropic_provider_aclose_closes_client(self) -> None:
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        with patch("corpclaw_lite.llm.anthropic.anthropic") as mock_mod:
            mock_mod.AsyncAnthropic.return_value = mock_client
            provider = AnthropicProvider(_anthropic_settings())

        await provider.aclose()
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_openai_provider_implements_async_closeable(self) -> None:
        from corpclaw_lite.llm.base import AsyncCloseable
        from corpclaw_lite.llm.openai import OpenAIProvider

        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        with patch("corpclaw_lite.llm.openai.openai") as mock_mod:
            mock_mod.AsyncOpenAI.return_value = mock_client
            provider = OpenAIProvider(_openai_settings())

        assert isinstance(provider, AsyncCloseable)

    @pytest.mark.asyncio
    async def test_anthropic_provider_implements_async_closeable(self) -> None:
        from corpclaw_lite.llm.anthropic import AnthropicProvider
        from corpclaw_lite.llm.base import AsyncCloseable

        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        with patch("corpclaw_lite.llm.anthropic.anthropic") as mock_mod:
            mock_mod.AsyncAnthropic.return_value = mock_client
            provider = AnthropicProvider(_anthropic_settings())

        assert isinstance(provider, AsyncCloseable)


# ── S1-02: timeout/retry wiring ───────────────────────────────────────────────────


class TestProviderTimeoutRetry:
    """S1-02: provider constructors forward timeout/max_retries to the SDK client."""

    def test_openai_provider_passes_timeout_and_max_retries(self) -> None:
        import httpx

        from corpclaw_lite.llm.openai import OpenAIProvider

        settings = ProviderSettings(
            type="openai",
            model="qwen2.5:7b",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
            connect_timeout=5.0,
            read_timeout=300.0,
            write_timeout=250.0,
            pool_timeout=200.0,
            max_retries=3,
        )
        with patch("corpclaw_lite.llm.openai.openai") as mock_mod:
            mock_mod.AsyncOpenAI.return_value = MagicMock()
            OpenAIProvider(settings)

        kwargs = mock_mod.AsyncOpenAI.call_args.kwargs
        assert isinstance(kwargs["timeout"], httpx.Timeout)
        assert kwargs["timeout"].write == 250.0
        assert kwargs["timeout"].pool == 200.0
        assert kwargs["max_retries"] == 3

    def test_anthropic_provider_passes_timeout_and_max_retries(self) -> None:
        import httpx

        from corpclaw_lite.llm.anthropic import AnthropicProvider

        settings = ProviderSettings(
            type="anthropic",
            model="claude-3-haiku-20240307",
            api_key="sk-ant-test123",
            connect_timeout=7.0,
            read_timeout=120.0,
            write_timeout=110.0,
            pool_timeout=90.0,
            max_retries=2,
        )
        with patch("corpclaw_lite.llm.anthropic.anthropic") as mock_mod:
            mock_mod.AsyncAnthropic.return_value = MagicMock()
            AnthropicProvider(settings)

        kwargs = mock_mod.AsyncAnthropic.call_args.kwargs
        assert isinstance(kwargs["timeout"], httpx.Timeout)
        assert kwargs["timeout"].write == 110.0
        assert kwargs["timeout"].pool == 90.0
        assert kwargs["max_retries"] == 2

    def test_default_timeouts_when_unset(self) -> None:
        """Defaults match the OpenAI/Anthropic SDK exactly (no regression)."""
        from corpclaw_lite.config.providers import ProviderConnection

        conn = ProviderConnection(type="openai", api_key="x", base_url="http://x")
        # Defaults mirror the SDK: connect 5s, read/write/pool 600s, 2 retries.
        assert conn.connect_timeout == 5.0
        assert conn.read_timeout == 600.0
        assert conn.write_timeout == 600.0
        assert conn.pool_timeout == 600.0
        assert conn.max_retries == 2
        # Regression guard: write/pool must NOT be tied to connect (a prior bug
        # set write=connect=10s, breaking large request bodies for all deploys).
        assert conn.write_timeout != conn.connect_timeout
        assert conn.pool_timeout != conn.connect_timeout
