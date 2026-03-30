"""Tests for Telegram progress indicator."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.channels.telegram.progress import StatusMessageSession


@pytest.mark.asyncio
async def test_status_message_session_lifecycle():
    mock_bot = AsyncMock()
    mock_message = AsyncMock()
    mock_sent_message = AsyncMock()
    mock_message.reply_text.return_value = mock_sent_message

    session = StatusMessageSession(
        bot=mock_bot,
        source_message=mock_message,
        chat_id=123,
    )

    # Start -> reply_text called
    await session.start()
    mock_message.reply_text.assert_called_once_with("⏳ В обработке...")

    # Mark tool start
    session.mark_tool_start("read_file")
    assert session._desired_text == "📂 Читаю файл..."

    # Close -> delete called
    await session.close()
    mock_sent_message.delete.assert_called_once()


@pytest.mark.asyncio
async def test_status_message_session_standalone():
    mock_bot = AsyncMock()
    mock_sent_message = AsyncMock()
    mock_bot.send_message.return_value = mock_sent_message

    session = StatusMessageSession(
        bot=mock_bot,
        source_message=None,
        chat_id=123,
        delete_on_finish=False,
    )

    await session.start_standalone()
    mock_bot.send_message.assert_called_once_with(chat_id=123, text="⏳ В обработке...")

    session.mark_tool_start("unknown_tool")
    assert session._desired_text == "⚙️ Выполняю действие..."

    await session.close()
    mock_sent_message.edit_text.assert_called_once_with("✅ Готово...")


@pytest.mark.asyncio
async def test_status_worker_loop():
    mock_bot = AsyncMock()
    mock_message = AsyncMock()
    mock_sent_message = AsyncMock()
    mock_message.reply_text.return_value = mock_sent_message

    session = StatusMessageSession(
        bot=mock_bot,
        source_message=mock_message,
        chat_id=123,
        update_interval_seconds=0.1,
        typing_heartbeat_seconds=0.1,
    )

    await session.start()

    # Let the worker loop run a bit
    await asyncio.sleep(0.3)

    # Should have sent a typing action
    mock_bot.send_chat_action.assert_called_with(chat_id=123, action="typing")

    session.mark_tool_start("web_fetch")

    # Let it sync
    await asyncio.sleep(0.3)

    # The message should have been edited
    mock_sent_message.edit_text.assert_called_with("🌐 Ищу информацию...")

    await session.close()
