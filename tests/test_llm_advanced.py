"""Tests for LLM providers advanced features (vision, streaming) and health server."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.config.settings import ProviderSettings
from corpclaw_lite.llm.anthropic import AnthropicProvider
from corpclaw_lite.llm.openai import OpenAIProvider


def _anthropic_settings() -> ProviderSettings:
    return ProviderSettings(type="anthropic", model="test-model", api_key="sk-ant")


def _openai_settings() -> ProviderSettings:
    return ProviderSettings(type="openai", model="test-model", api_key="sk-abc")


# ── Vision ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_chat_with_image() -> None:
    mock_client = AsyncMock()
    block = MagicMock()
    block.type = "text"
    block.text = "I see a cat."
    mock_msg = MagicMock()
    mock_msg.content = [block]
    mock_msg.usage.input_tokens = 5
    mock_msg.usage.output_tokens = 4
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    with patch("corpclaw_lite.llm.anthropic.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(_anthropic_settings())
        resp = await provider.chat_with_image(
            image_data="base64data",
            image_media_type="image/jpeg",
            prompt="What is this?",
            system="You are an AI.",
        )

    assert resp.content == "I see a cat."
    args = mock_client.messages.create.await_args.kwargs
    assert args["system"] == "You are an AI."
    # Ensure message format
    assert args["messages"][0]["content"][0]["type"] == "image"


@pytest.mark.asyncio
async def test_openai_chat_with_image() -> None:
    mock_client = AsyncMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "It is a dog."
    mock_response = MagicMock(choices=[mock_choice])
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch("corpclaw_lite.llm.openai.openai.AsyncOpenAI", return_value=mock_client):
        provider = OpenAIProvider(_openai_settings())
        resp = await provider.chat_with_image(
            image_data="base64data",
            image_media_type="image/png",
            prompt="Analyze this",
            system="Sys prompt",
        )

    assert resp.content == "It is a dog."
    args = mock_client.chat.completions.create.await_args.kwargs
    assert args["messages"][0]["role"] == "system"
    assert args["messages"][1]["content"][0]["type"] == "image_url"


# ── Streaming ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_stream() -> None:
    mock_client = AsyncMock()

    class MockStreamContext:
        async def __aenter__(self):
            class _Inner:
                async def _text_stream(self):
                    yield "hello "
                    yield "world!"

                @property
                def text_stream(self):
                    return self._text_stream()

            return _Inner()

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_client.messages.stream = MagicMock(return_value=MockStreamContext())

    with patch("corpclaw_lite.llm.anthropic.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(_anthropic_settings())
        chunks = []
        async for chunk in provider.stream([{"role": "user", "content": "Hi"}], system="Hi"):
            chunks.append(chunk.content)

    assert chunks == ["hello ", "world!"]


@pytest.mark.asyncio
async def test_openai_stream() -> None:
    mock_client = AsyncMock()

    async def _fake_stream(*args, **kwargs):
        class _Chunk:
            def __init__(self, text):
                self.choices = [MagicMock()]
                self.choices[0].delta.content = text

        yield _Chunk("Hey! ")
        yield _Chunk("There.")

    mock_client.chat.completions.create = AsyncMock(return_value=_fake_stream())

    with patch("corpclaw_lite.llm.openai.openai.AsyncOpenAI", return_value=mock_client):
        provider = OpenAIProvider(_openai_settings())
        chunks = []
        async for chunk in provider.stream([{"role": "user", "content": "Hi"}], system="Hi"):
            chunks.append(chunk.content)

    assert chunks == ["Hey! ", "There."]


# ── _resolve_reasoning_fallback ───────────────────────────────────────────────


def _make_provider() -> OpenAIProvider:
    with patch("corpclaw_lite.llm.openai.openai.AsyncOpenAI"):
        return OpenAIProvider(_openai_settings())


def _raw_msg(
    *,
    content: str = "",
    tool_calls: object = None,
    reasoning_content: str = "",
) -> object:
    """Build a minimal fake message object matching what OpenAI SDK returns."""
    from types import SimpleNamespace

    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )


def test_resolve_reasoning_fallback_no_op_content_present() -> None:
    """If content is already set, fallback must not fire."""
    provider = _make_provider()
    msg = _raw_msg(content="Hello!", reasoning_content="some reasoning")
    content, tool_calls = provider._resolve_reasoning_fallback("Hello!", "stop", msg, None, [])
    assert content == "Hello!"
    assert tool_calls == []


def test_resolve_reasoning_fallback_plain_text_no_tools() -> None:
    """Empty content + reasoning_content + no tools → reasoning becomes answer."""
    provider = _make_provider()
    msg = _raw_msg(reasoning_content="Here is my answer.")
    content, tool_calls = provider._resolve_reasoning_fallback("", "stop", msg, None, [])
    assert content == "Here is my answer."
    assert tool_calls == []


def test_resolve_reasoning_fallback_xml_tool_call_extracted() -> None:
    """XML tool call in reasoning_content → extracted as ToolCall, content cleared."""
    provider = _make_provider()
    xml = '<tool_call><name>read_file</name><arguments>{"path": "foo.txt"}</arguments></tool_call>'
    msg = _raw_msg(reasoning_content=xml)
    tools = [{"function": {"name": "read_file", "parameters": {}}}]
    content, tool_calls = provider._resolve_reasoning_fallback("", "stop", msg, tools, [])
    # Content must be cleared when a valid tool call was found
    assert content == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "read_file"


def test_resolve_reasoning_fallback_unparseable_xml_falls_back_to_text() -> None:
    """XML markers present but parse fails → treat reasoning as text content."""
    provider = _make_provider()
    bad_xml = "<tool_call>BROKEN XML</tool_call>"
    msg = _raw_msg(reasoning_content=bad_xml)
    tools = [{"function": {"name": "some_tool", "parameters": {}}}]
    content, tool_calls = provider._resolve_reasoning_fallback("", "stop", msg, tools, [])
    assert content == bad_xml.strip()
    assert tool_calls == []


# ── Anthropic Preset Bugs ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_thinking_budget_overrides_max_tokens() -> None:
    """thinking_budget_tokens must override provider default max_tokens=4096."""
    from corpclaw_lite.llm.presets import ModelPreset

    mock_client = AsyncMock()
    block = MagicMock()
    block.type = "text"
    block.text = "response"
    mock_msg = MagicMock()
    mock_msg.content = [block]
    mock_msg.usage.input_tokens = 1
    mock_msg.usage.output_tokens = 1
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    preset = ModelPreset(thinking_budget_tokens=2048)
    with patch("corpclaw_lite.llm.anthropic.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(_anthropic_settings(), preset=preset)
        await provider.chat([{"role": "user", "content": "hi"}])

    args = mock_client.messages.create.await_args.kwargs
    assert args["max_tokens"] == 3072, f"Expected 3072 (2048+1024), got {args['max_tokens']}"


@pytest.mark.asyncio
async def test_anthropic_chat_with_image_applies_system_prompt_prefix() -> None:
    """chat_with_image must apply system_prompt_prefix from preset."""
    from corpclaw_lite.llm.presets import ModelPreset

    mock_client = AsyncMock()
    block = MagicMock()
    block.type = "text"
    block.text = "desc"
    mock_msg = MagicMock()
    mock_msg.content = [block]
    mock_msg.usage.input_tokens = 1
    mock_msg.usage.output_tokens = 1
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    preset = ModelPreset(system_prompt_prefix="<|think|>")
    with patch("corpclaw_lite.llm.anthropic.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(_anthropic_settings(), preset=preset)
        await provider.chat_with_image(
            image_data="data", image_media_type="image/png", prompt="What?", system="You are AI."
        )

    args = mock_client.messages.create.await_args.kwargs
    assert "<|think|>" in args["system"]
    assert "You are AI." in args["system"]


@pytest.mark.asyncio
async def test_anthropic_stream_applies_system_prompt_prefix() -> None:
    """stream() must apply system_prompt_prefix from preset."""

    from corpclaw_lite.llm.presets import ModelPreset

    mock_client = AsyncMock()

    class MockStreamContext:
        async def __aenter__(self):
            class _Inner:
                async def _text_stream(self):
                    yield "chunk"

                @property
                def text_stream(self):
                    return self._text_stream()

            return _Inner()

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_client.messages.stream = MagicMock(return_value=MockStreamContext())

    preset = ModelPreset(system_prompt_prefix="<|think|>")
    with patch("corpclaw_lite.llm.anthropic.anthropic.AsyncAnthropic", return_value=mock_client):
        provider = AnthropicProvider(_anthropic_settings(), preset=preset)
        chunks = []
        async for chunk in provider.stream(
            [{"role": "user", "content": "Hi"}], system="Base system"
        ):
            chunks.append(chunk.content)

    call_kwargs = mock_client.messages.stream.call_args.kwargs
    assert "<|think|>" in call_kwargs["system"]
    assert "Base system" in call_kwargs["system"]
