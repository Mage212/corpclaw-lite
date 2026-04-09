"""Tests for memory consolidation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.memory.consolidation import MemoryConsolidator
from corpclaw_lite.memory.sqlite import SQLiteMemory


@pytest.fixture
def memory(tmp_path):
    """Create a temp SQLiteMemory."""
    return SQLiteMemory(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def mock_provider():
    """Create a mock LLM provider."""
    provider = MagicMock()
    response = MagicMock()
    response.content = "- User asked about Excel files\n- Agent normalized two spreadsheets"
    provider.chat = AsyncMock(return_value=response)
    return provider


@pytest.mark.asyncio
async def test_count_messages(memory):
    """Test message counting."""
    assert await memory.count_messages("u1") == 0
    await memory.add_message("u1", "user", "Hello")
    assert await memory.count_messages("u1") == 1
    await memory.add_message("u1", "assistant", "Hi!")
    assert await memory.count_messages("u1") == 2
    # Different user doesn't affect count
    await memory.add_message("u2", "user", "Hey")
    assert await memory.count_messages("u1") == 2


@pytest.mark.asyncio
async def test_get_oldest_message_ids(memory):
    """Test getting oldest message IDs."""
    for i in range(5):
        await memory.add_message("u1", "user", f"Message {i}")
    ids = await memory.get_oldest_message_ids("u1", 3)
    assert len(ids) == 3


@pytest.mark.asyncio
async def test_replace_oldest(memory):
    """Test replacing oldest messages with summary."""
    for i in range(6):
        await memory.add_message("u1", "user", f"Message {i}")

    assert await memory.count_messages("u1") == 6
    await memory.replace_oldest("u1", count=3, summary="Summary of first 3 messages")

    # 6 - 3 deleted + 1 summary = 4
    assert await memory.count_messages("u1") == 4
    history = await memory.get_history("u1", limit=10)
    # Summary should appear somewhere in the history
    all_content = " ".join(str(m["content"]) for m in history)
    assert "[Conversation summary]" in all_content


@pytest.mark.asyncio
async def test_consolidator_below_threshold(memory, mock_provider):
    """No consolidation when below threshold."""
    await memory.add_message("u1", "user", "Hello")
    consolidator = MemoryConsolidator(mock_provider, threshold=10)
    result = await consolidator.maybe_consolidate(memory, "u1")
    assert result is False
    mock_provider.chat.assert_not_called()


@pytest.mark.asyncio
async def test_consolidator_above_threshold(memory, mock_provider):
    """Consolidation triggers when above threshold."""
    for i in range(12):
        await memory.add_message("u1", "user", f"Message {i}")
        await memory.add_message("u1", "assistant", f"Reply {i}")

    consolidator = MemoryConsolidator(mock_provider, threshold=20)
    result = await consolidator.maybe_consolidate(memory, "u1")
    assert result is True
    mock_provider.chat.assert_called_once()
    # Messages should be reduced
    assert await memory.count_messages("u1") < 24


@pytest.mark.asyncio
async def test_consolidator_formats_messages(memory, mock_provider):
    """Verify the summarization prompt includes message content."""
    for i in range(8):
        await memory.add_message("u1", "user", f"Question {i}")
        await memory.add_message("u1", "assistant", f"Answer {i}")

    consolidator = MemoryConsolidator(mock_provider, threshold=10)
    await consolidator.maybe_consolidate(memory, "u1")

    # The LLM should have been called with messages containing the conversation
    call_args = mock_provider.chat.call_args
    prompt = call_args.kwargs["messages"][0]["content"]
    assert "Question" in prompt
    assert "Answer" in prompt


@pytest.mark.asyncio
async def test_consolidator_cooldown(memory, mock_provider):
    """Second consolidation within the cooldown window is skipped."""
    for i in range(12):
        await memory.add_message("u1", "user", f"Message {i}")
        await memory.add_message("u1", "assistant", f"Reply {i}")

    consolidator = MemoryConsolidator(mock_provider, threshold=20)
    # First consolidation should succeed
    first = await consolidator.maybe_consolidate(memory, "u1")
    assert first is True

    # Re-populate above threshold
    for i in range(12):
        await memory.add_message("u1", "user", f"More {i}")
        await memory.add_message("u1", "assistant", f"Extra {i}")

    # Second consolidation within 60s cooldown should be skipped
    second = await consolidator.maybe_consolidate(memory, "u1")
    assert second is False
    # LLM was only called once (first consolidation)
    assert mock_provider.chat.call_count == 1


@pytest.mark.asyncio
async def test_consolidator_skips_on_active_workflow_marker(memory, mock_provider):
    """Consolidation is skipped when recent messages contain tool markers."""
    for i in range(12):
        await memory.add_message("u1", "user", f"Message {i}")
        await memory.add_message("u1", "assistant", f"Reply {i}")

    # Add a message with a tool-call marker in the tail
    await memory.add_message("u1", "assistant", "[Called tools: web_fetch] fetching...")

    consolidator = MemoryConsolidator(mock_provider, threshold=20)
    result = await consolidator.maybe_consolidate(memory, "u1")
    assert result is False
    mock_provider.chat.assert_not_called()
