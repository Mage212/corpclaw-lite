"""Tests for LLM providers advanced features (vision, streaming) and health server."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.config.settings import ProviderSettings
from corpclaw_lite.llm.base import LLMResponse, StreamChunk
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
            system="You are an AI."
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
            system="Sys prompt"
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


# ── Health Endpoint ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_health_server() -> None:
    pytest.importorskip("aiohttp")
    
    from corpclaw_lite.logging import health
    from aiohttp import web
    
    # We don't want to actually bind to a port during unit tests, so we mock runner
    with patch("aiohttp.web.AppRunner.setup", new_callable=AsyncMock) as mock_setup, \
         patch("aiohttp.web.TCPSite.start", new_callable=AsyncMock) as mock_start:
             
        await health.run_health_server(host="127.0.0.1", port=9999)
        mock_setup.assert_awaited_once()
        mock_start.assert_awaited_once()

