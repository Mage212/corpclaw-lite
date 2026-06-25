"""Tests for OpenAIProvider._resolve_reasoning_fallback — length gate (FIX 2).

Regression coverage: gemma4 at thinking-OFF + low temperature non-deterministically
produces a short (12-char) garbage reasoning fragment. The old code copied any
reasoning into content, creating a truncated XML marker that triggered a false
``malformed_xml_tool_call`` crash. The length gate now leaves content empty for
short fragments so the agent retries instead of crashing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.llm.openai import OpenAIProvider

# ── Helpers ──────────────────────────────────────────────────────────────────


class _StubTool(Tool):
    name = "research_search"
    description = "search"
    params = [ToolParam(name="query", type="string", description="q")]
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        return ""


def _make_provider() -> OpenAIProvider:
    """Build an OpenAIProvider with a native thinking_parser profile."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.presets import ModelProfile, SamplingProfile, ThinkingConfig

    settings = ProviderSettings(model="test-model", base_url="http://x/v1", api_key="k")
    return OpenAIProvider(
        settings,
        model_profile=ModelProfile(
            description="test",
            thinking_parser=ThinkingConfig(source="native"),
            default_inference={"temperature": 1.0},
        ),
        sampling=SamplingProfile(description="s", model="test-model", thinking_mode="default"),
    )


def _tools_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "research_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]


# ── Tests ────────────────────────────────────────────────────────────────────


def test_short_reasoning_not_copied_to_content() -> None:
    """Short reasoning fragment (<100 chars) is NOT copied to content.

    Regression: gemma4's 12-char garbage reasoning was copied to content,
    creating a malformed XML marker → false crash.
    """
    provider = _make_provider()
    raw = SimpleNamespace(content="", reasoning_content="<tool_call>ab", tool_calls=None)
    content, tool_calls = provider._resolve_reasoning_fallback(
        content="",
        finish_reason="stop",
        raw_message=raw,
        tools=_tools_schema(),
        tool_calls=[],
    )
    # Content stays empty — agent can retry instead of crashing on garbage.
    assert content == ""
    assert tool_calls == []


def test_substantial_plain_reasoning_copied_to_content() -> None:
    """Substantial plain-text reasoning (>=100 chars) IS used as the answer.

    This is the Qwen3 edge case: model puts the whole answer in reasoning_content.
    """
    provider = _make_provider()
    long_reasoning = (
        "The answer to the question is that FlashAttention uses tiling to reduce memory access. "
        * 3
    )
    assert len(long_reasoning) >= 100
    raw = SimpleNamespace(content="", reasoning_content=long_reasoning, tool_calls=None)
    content, tool_calls = provider._resolve_reasoning_fallback(
        content="",
        finish_reason="stop",
        raw_message=raw,
        tools=None,
        tool_calls=[],
    )
    assert content == long_reasoning.strip()


def test_substantial_xml_reasoning_in_tools_parsed() -> None:
    """Substantial reasoning with XML tool markers → parsed as tool call (sub-case a)."""

    provider = _make_provider()
    xml = (
        "<tool_call><name>research_search</name>"
        '<arguments>{"query": "flashattention mechanism"}</arguments></tool_call>'
    )
    # Pad to be above threshold (sub-case a parses regardless of length, but test
    # the realistic case).
    raw = SimpleNamespace(content="", reasoning_content=xml, tool_calls=None)
    content, tool_calls = provider._resolve_reasoning_fallback(
        content="",
        finish_reason="stop",
        raw_message=raw,
        tools=_tools_schema(),
        tool_calls=[],
    )
    assert content == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "research_search"


def test_no_fallback_when_content_present() -> None:
    """If content is non-empty, no fallback (normal path)."""
    provider = _make_provider()
    raw = SimpleNamespace(content="real answer", reasoning_content="ignored", tool_calls=None)
    content, tool_calls = provider._resolve_reasoning_fallback(
        content="real answer",
        finish_reason="stop",
        raw_message=raw,
        tools=_tools_schema(),
        tool_calls=[],
    )
    assert content == "real answer"


def test_no_fallback_when_finish_not_stop() -> None:
    """finish_reason=tool_calls → no reasoning fallback (native tool calls path)."""
    provider = _make_provider()
    raw = SimpleNamespace(content="", reasoning_content="some reasoning", tool_calls=None)
    content, tool_calls = provider._resolve_reasoning_fallback(
        content="",
        finish_reason="tool_calls",
        raw_message=raw,
        tools=_tools_schema(),
        tool_calls=[],
    )
    assert content == ""
