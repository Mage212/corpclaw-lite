"""Tests for Telegram progress indicator — StatusMessageSession."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.channels.telegram.progress import _TOOL_STATUS_MAP, StatusMessageSession


@pytest.fixture
def mock_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    bot.send_chat_action = AsyncMock()
    return bot


@pytest.fixture
def mock_source_message() -> MagicMock:
    msg = MagicMock()
    msg.reply_text = AsyncMock(return_value=MagicMock())
    return msg


@pytest.fixture
def session(mock_bot: MagicMock, mock_source_message: MagicMock) -> StatusMessageSession:
    return StatusMessageSession(
        bot=mock_bot,
        source_message=mock_source_message,
        chat_id=12345,
    )


@pytest.mark.asyncio
async def test_start_sends_reply(
    session: StatusMessageSession, mock_source_message: MagicMock
) -> None:
    """start() should reply to the source message and launch the background worker."""
    await session.start()
    mock_source_message.reply_text.assert_called_once_with("⏳ В обработке...")
    assert session._message is not None
    assert session._worker_task is not None
    await session.close()


@pytest.mark.asyncio
async def test_start_standalone_sends_message(
    session: StatusMessageSession, mock_bot: MagicMock
) -> None:
    """start_standalone() should send a new message (not a reply)."""
    await session.start_standalone()
    mock_bot.send_message.assert_called_once_with(chat_id=12345, text="⏳ В обработке...")
    assert session._message is not None
    await session.close()


@pytest.mark.asyncio
async def test_mark_tool_start_updates_desired_text(session: StatusMessageSession) -> None:
    """mark_tool_start() should set the desired text based on tool name."""
    session.mark_tool_start("read_file")
    assert session._desired_text == "📂 Читаю файл..."

    session.mark_tool_start("web_fetch")
    assert session._desired_text == "🌐 Ищу информацию..."

    session.mark_tool_start("unknown_tool")
    assert session._desired_text == "⚙️ Выполняю действие..."


@pytest.mark.asyncio
async def test_mark_tool_start_noop_when_closed(session: StatusMessageSession) -> None:
    """mark_tool_start() should be a no-op after close()."""
    session._closed = True
    session.mark_tool_start("read_file")
    assert session._desired_text == "⏳ В обработке..."  # unchanged


@pytest.mark.asyncio
async def test_close_deletes_message(session: StatusMessageSession) -> None:
    """close() should delete the message when delete_on_finish=True (default)."""
    mock_msg = MagicMock()
    mock_msg.delete = AsyncMock()
    session._message = mock_msg

    await session.close()
    mock_msg.delete.assert_called_once()


@pytest.mark.asyncio
async def test_close_edits_done_when_no_delete(
    mock_bot: MagicMock, mock_source_message: MagicMock
) -> None:
    """close() should edit to 'done' when delete_on_finish=False."""
    s = StatusMessageSession(
        bot=mock_bot,
        source_message=mock_source_message,
        chat_id=12345,
        delete_on_finish=False,
    )
    mock_msg = MagicMock()
    mock_msg.edit_text = AsyncMock()
    s._message = mock_msg

    await s.close()
    mock_msg.edit_text.assert_called_once_with("✅ Готово...")


@pytest.mark.asyncio
async def test_close_without_message(session: StatusMessageSession) -> None:
    """close() should handle case when no message was ever sent."""
    await session.close()  # should not raise


@pytest.mark.asyncio
async def test_close_cancels_worker(
    session: StatusMessageSession, mock_source_message: MagicMock
) -> None:
    """close() should cancel the background worker task."""
    await session.start()
    assert session._worker_task is not None
    assert not session._worker_task.done()

    await session.close()
    assert session._worker_task is None
    assert session._closed


def test_tool_status_map_coverage() -> None:
    """All expected tools should have friendly status strings."""
    expected_tools = [
        "list_files",
        "read_file",
        "read_image",
        "normalize_excel",
        "web_fetch",
        "exec_script",
        "write_file",
        "edit_file",
        "search_files",
        "memory_store",
        "memory_recall",
        "dispatch_subagent",
    ]
    for tool in expected_tools:
        assert tool in _TOOL_STATUS_MAP


@pytest.mark.asyncio
async def test_init_clamps_values() -> None:
    """Constructor should clamp interval and max_updates to sane minimums."""
    s = StatusMessageSession(
        bot=MagicMock(),
        source_message=MagicMock(),
        chat_id=1,
        update_interval_seconds=0.1,  # below min
        typing_heartbeat_seconds=0.1,  # below min
        max_updates_per_request=-5,  # below min
    )
    assert s._update_interval_seconds >= 0.5
    assert s._typing_heartbeat_seconds >= 1.0
    assert s._max_updates_per_request >= 0
