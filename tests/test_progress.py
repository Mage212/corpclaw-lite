"""Tests for Telegram progress indicator — StatusMessageSession."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.channels.status import (
    format_llm_queue_status,
    format_llm_stage_status,
    format_subagent_llm_queue_status,
    format_subagent_llm_stage_status,
    format_subagent_tool_batch_status,
    format_subagent_tool_status,
    format_tool_batch_status,
)
from corpclaw_lite.channels.telegram.progress import _TOOL_STATUS_MAP, StatusMessageSession
from corpclaw_lite.llm.queue import LLMQueueStatus


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
async def test_mark_llm_stage_updates_desired_text(session: StatusMessageSession) -> None:
    """mark_llm_stage() should expose coarse backend LLM stages."""
    session.mark_llm_stage("model_waiting")
    assert session._desired_text == "⏳ Жду начало ответа модели..."

    session.mark_llm_stage("reasoning")
    assert session._desired_text == "🤔 Думаю..."

    session.mark_llm_stage("tool_call")
    assert session._desired_text == "⚙️ Готовлю действие..."

    session.mark_llm_stage("answer")
    assert session._desired_text == "📝 Собираю ответ..."


@pytest.mark.asyncio
async def test_mark_tool_batch_start_updates_desired_text(session: StatusMessageSession) -> None:
    """mark_tool_batch_start() should expose one aggregate parallel-tools status."""
    session.mark_tool_batch_start(["read_file", "list_files"])
    assert session._desired_text == "📂 Работаю с файлами..."

    session.mark_tool_batch_start(["read_file", "web_fetch"])
    assert session._desired_text == "⚙️ Выполняю 2 действия..."


def test_tool_batch_status_formatter_groups_same_kind_tools() -> None:
    assert format_tool_batch_status(["web_fetch", "web_search"]) == "🔍 Ищу данные..."
    assert format_tool_batch_status(["memory_store", "memory_recall"]) == "💾 Работаю с памятью..."


def test_finished_llm_stage_has_no_user_status() -> None:
    assert format_llm_stage_status("finished") is None


@pytest.mark.asyncio
async def test_mark_subagent_status_updates_desired_text(
    session: StatusMessageSession,
) -> None:
    """Subagent status callbacks should prefix the human-readable subagent name."""
    session.mark_subagent_llm_stage("Research Agent", "reasoning")
    assert session._desired_text == "Research Agent: 🤔 Думаю..."

    session.mark_subagent_tool_start("Document Agent", "read_file")
    assert session._desired_text == "Document Agent: 📂 Читаю файл..."

    session.mark_subagent_tool_batch_start("Data Analysis Agent", ["read_file", "list_files"])
    assert session._desired_text == "Data Analysis Agent: 📂 Работаю с файлами..."


@pytest.mark.asyncio
async def test_mark_llm_queue_status_updates_desired_text(session: StatusMessageSession) -> None:
    status = LLMQueueStatus(
        user_id="1",
        task_kind="default",
        load_class="interactive",
        position=1,
        estimated_wait_seconds=32.8,
        waiting_count=2,
        active_count=1,
        max_concurrent=1,
        wait_seconds=3.0,
    )

    session.mark_llm_queue_status(status)
    assert session._desired_text == "⏳ Ожидаю LLM-слот. В очереди: 2, примерно 32с..."

    session.mark_subagent_llm_queue_status("Research Agent", status)
    assert (
        session._desired_text == "Research Agent: ⏳ Ожидаю LLM-слот. В очереди: 2, примерно 32с..."
    )


def test_subagent_status_formatters() -> None:
    assert (
        format_subagent_tool_status("Execution Agent", "exec_script")
        == "Execution Agent: 💻 Запускаю команду..."
    )
    assert (
        format_subagent_tool_batch_status("Research Agent", ["web_fetch", "web_search"])
        == "Research Agent: 🔍 Ищу данные..."
    )
    assert (
        format_subagent_llm_stage_status("Document Agent", "answer")
        == "Document Agent: 📝 Собираю ответ..."
    )
    assert format_subagent_llm_stage_status("Document Agent", "finished") is None

    status = LLMQueueStatus(
        user_id="1",
        task_kind="default",
        load_class="interactive",
        position=None,
        estimated_wait_seconds=None,
        waiting_count=1,
        active_count=1,
        max_concurrent=1,
        wait_seconds=1.0,
    )
    assert format_llm_queue_status(status) == "⏳ Ожидаю свободный LLM-слот..."
    assert (
        format_subagent_llm_queue_status("Document Agent", status)
        == "Document Agent: ⏳ Ожидаю свободный LLM-слот..."
    )


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
