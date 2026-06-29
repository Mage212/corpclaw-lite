from typing import Any
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.agent.compressor import ContextCompressor
from corpclaw_lite.config.settings import CompressionSettings
from corpclaw_lite.llm.base import LLMResponse


class MockProvider:
    def __init__(self, response: str = "Mock summary"):
        self._response = response
        self.calls: list[list[dict]] = []

    async def chat(self, messages: list[dict[str, Any]], tools=None, system=None):
        self.calls.append(messages)
        return LLMResponse(content=self._response)


@pytest.fixture
def settings() -> CompressionSettings:
    return CompressionSettings(
        enabled=True,
        max_context_tokens=1000,
        threshold_ratio=0.5,
        protect_tail_tokens=200,
        summary_ratio=0.20,
    )


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


class TestShouldCompress:
    def test_disabled_returns_false(self, provider: MockProvider) -> None:
        settings = CompressionSettings(enabled=False)
        compressor = ContextCompressor(provider, settings)
        messages = [{"role": "user", "content": "x" * 10000}]
        assert not compressor.should_compress(messages)

    def test_below_threshold_returns_false(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [{"role": "user", "content": "short"}]
        assert not compressor.should_compress(messages)

    def test_above_threshold_returns_true(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [{"role": "user", "content": "x" * 3000}]
        assert compressor.should_compress(messages)

    def test_actual_tokens_can_trigger_compression(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [{"role": "user", "content": "short"}]
        assert compressor.should_compress(messages, actual_tokens=900)

    def test_does_not_compress_when_last_message_is_tool_result(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        """Regression: compressor must not fire mid-ReAct between web_fetch and LLM processing.

        When the last context message is a tool result (role=tool), the agent hasn't
        yet processed the output. Compressing at this point generates a summary in
        place of the real answer and causes the task to be abandoned.
        """
        compressor = ContextCompressor(provider, settings)
        # Even with massive content that clearly exceeds the threshold...
        messages = [
            {"role": "user", "content": "x" * 3000},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "function": {"name": "web_fetch", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "1", "name": "web_fetch", "content": "x" * 3000},
        ]
        # ...it must NOT compress because the last message is a pending tool result
        assert not compressor.should_compress(messages)


class TestSanitizeToolPairs:
    def test_no_orphans(self, provider: MockProvider, settings: CompressionSettings) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "1", "name": "t", "content": "result"},
        ]
        result = compressor._sanitize_tool_pairs(messages)
        assert len(result) == 2

    def test_removes_orphaned_tool_result(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [
            {"role": "tool", "tool_call_id": "orphan", "name": "t", "content": "result"},
        ]
        result = compressor._sanitize_tool_pairs(messages)
        assert len(result) == 0

    def test_adds_stub_for_orphaned_tool_call(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "orphan",
                        "type": "function",
                        "function": {"name": "test_tool", "arguments": "{}"},
                    }
                ],
            },
        ]
        result = compressor._sanitize_tool_pairs(messages)
        assert len(result) == 2
        assert result[1]["role"] == "tool"
        assert result[1]["tool_call_id"] == "orphan"
        assert "lost" in result[1]["content"].lower()


class TestCompress:
    @pytest.mark.asyncio
    async def test_skips_small_messages(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        result = await compressor.compress(messages)
        assert result == messages

    @pytest.mark.asyncio
    async def test_compress_generates_summary(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        settings = CompressionSettings(
            enabled=True,
            max_context_tokens=300,
            threshold_ratio=0.5,
            protect_tail_tokens=50,
            summary_ratio=0.20,
        )
        compressor = ContextCompressor(provider, settings)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "x" * 200},
            {"role": "assistant", "content": "y" * 200},
            {"role": "user", "content": "z" * 200},
            {"role": "assistant", "content": "w" * 200},
            {"role": "user", "content": "a" * 200},
            {"role": "assistant", "content": "b" * 200},
            {"role": "user", "content": "final"},
        ]
        result = await compressor.compress(messages)

        assert len(result) < len(messages)
        summary_msgs = [m for m in result if "Summary" in m.get("content", "")]
        assert len(summary_msgs) == 1

    @pytest.mark.asyncio
    async def test_compress_uses_threshold_ratio_not_full_context_limit(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        settings = CompressionSettings(
            enabled=True,
            max_context_tokens=1000,
            threshold_ratio=0.8,
            protect_tail_tokens=50,
            summary_ratio=0.20,
        )
        compressor = ContextCompressor(provider, settings)
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "x" * 600},
            {"role": "user", "content": "y" * 700},
            {"role": "assistant", "content": "z" * 700},
            {"role": "user", "content": "a" * 700},
            {"role": "assistant", "content": "b" * 700},
            {"role": "user", "content": "final"},
        ]

        assert compressor.should_compress(messages)
        result = await compressor.compress(messages)

        assert len(result) < len(messages)
        summary_msgs = [m for m in result if "Summary" in m.get("content", "")]
        assert len(summary_msgs) == 1

    @pytest.mark.asyncio
    async def test_compress_preserves_head_and_tail(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "x" * 400},
            {"role": "user", "content": "y" * 400},
            {"role": "assistant", "content": "z" * 400},
            {"role": "user", "content": "last user message"},
        ]
        result = await compressor.compress(messages)

        assert result[0]["role"] == "system"
        assert result[0]["content"] == "system prompt"


class TestGenerateSummary:
    @pytest.mark.asyncio
    async def test_generates_structured_summary(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        compressor = ContextCompressor(provider, settings)
        turns = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        summary = await compressor._generate_summary(turns)
        assert summary == "Mock summary"
        assert len(provider.calls) == 1

    @pytest.mark.asyncio
    async def test_uses_previous_summary(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        compressor = ContextCompressor(provider, settings)
        compressor._summaries["user_123"] = "Old summary"

        turns = [{"role": "user", "content": "New message"}]
        await compressor._generate_summary(turns, mem_key="user_123")

        prompt = provider.calls[0][0]["content"]
        assert "Previous summary" in prompt
        assert "Old summary" in prompt

    @pytest.mark.asyncio
    async def test_returns_none_on_provider_error(self, settings: CompressionSettings) -> None:
        failing_provider = AsyncMock()
        failing_provider.chat.side_effect = Exception("LLM error")

        compressor = ContextCompressor(failing_provider, settings)
        turns = [{"role": "user", "content": "test"}]

        result = await compressor._generate_summary(turns)
        assert result is None


class TestEstimateTokens:
    def test_empty_messages(self, provider: MockProvider, settings: CompressionSettings) -> None:
        compressor = ContextCompressor(provider, settings)
        assert compressor._estimate_tokens([]) == 0

    def test_counts_content(self, provider: MockProvider, settings: CompressionSettings) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [{"role": "user", "content": "x" * 400}]
        estimate = compressor._estimate_tokens(messages)
        assert estimate == 100

    def test_counts_tool_calls(self, provider: MockProvider, settings: CompressionSettings) -> None:
        compressor = ContextCompressor(provider, settings)
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{" + "x" * 400 + "}"},
                    }
                ],
            }
        ]
        estimate = compressor._estimate_tokens(messages)
        assert estimate > 0

    def test_cyrillic_token_estimation(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        """P1-3: Cyrillic text should produce higher token estimate than naive len/4.

        Cyrillic characters are 2 bytes in UTF-8, so the estimator should use
        a divisor of 2 instead of 4, roughly doubling the token estimate.
        """
        compressor = ContextCompressor(provider, settings)
        cyrillic_text = "Привет мир, это тестовое сообщение для проверки оценки токенов"
        latin_text = "a" * len(cyrillic_text)

        cyrillic_msgs = [{"role": "user", "content": cyrillic_text}]
        latin_msgs = [{"role": "user", "content": latin_text}]

        cyrillic_estimate = compressor._estimate_tokens(cyrillic_msgs)
        latin_estimate = compressor._estimate_tokens(latin_msgs)

        # Cyrillic estimate should be significantly higher than latin of same char count
        assert cyrillic_estimate > latin_estimate


class TestTailBoundaryToolPairs:
    """B-074/M3: the tail boundary must not split an assistant tool_calls
    message from its trailing tool result(s)."""

    def test_boundary_moves_back_to_include_assistant_caller(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        """When the token-budget boundary lands on a `tool` result, the boundary
        must move back so the originating `assistant` tool_calls message is also
        in the tail (otherwise the pair is split and the result is stubbed)."""
        compressor = ContextCompressor(provider, settings)
        big = "x" * 500  # large enough to dominate the tail token budget

        messages = [
            {"role": "user", "content": "q1"},  # 0
            {"role": "assistant", "content": "a1"},  # 1
            {"role": "user", "content": big},  # 2  (middle — will be summarized)
            {"role": "assistant", "content": big},  # 3  (middle)
            {
                # 4 — assistant issuing a tool call; must stay with its result
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "real result"},  # 5
            {"role": "user", "content": big},  # 6
        ]
        # With protect_tail_tokens=200, the budget lands around index 4-5.
        boundary = compressor._find_tail_boundary(messages, tail_budget_tokens=200)
        # The boundary must NOT be index 5 (would split the pair: assistant@4
        # in middle, tool@5 in tail). It must be <= 4 so the assistant caller
        # is included in the tail alongside its result.
        assert boundary <= 4
        assert str(messages[boundary].get("role", "")) != "tool"

    def test_boundary_unaffected_when_no_tool_pair_at_boundary(
        self, provider: MockProvider, settings: CompressionSettings
    ) -> None:
        """No adjustment when the boundary message is a plain user/assistant turn."""
        compressor = ContextCompressor(provider, settings)
        messages = [
            {"role": "user", "content": "q1"},  # 0
            {"role": "assistant", "content": "a1"},  # 1
            {"role": "user", "content": "x" * 500},  # 2
            {"role": "assistant", "content": "x" * 500},  # 3
            {"role": "user", "content": "x" * 500},  # 4
        ]
        boundary = compressor._find_tail_boundary(messages, tail_budget_tokens=200)
        # No tool messages here — the adjustment is a no-op.
        assert str(messages[boundary].get("role", "")) in ("user", "assistant")
