"""Tests for TelegramChannel core methods."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.constants import ParseMode

from corpclaw_lite.channels.telegram.channel import TelegramChannel
from corpclaw_lite.users.models import User


@pytest.fixture()
def mock_bot():
    return AsyncMock()


@pytest.fixture()
def mock_app(mock_bot):
    app = MagicMock()
    app.bot = mock_bot
    return app


@pytest.fixture()
def channel(mock_app):
    async def mock_handler(tid: str, text: str, mode: str = "execute") -> None:
        pass

    ch = TelegramChannel(
        token="test_token",
        message_handler=mock_handler,
    )
    ch._app = mock_app
    return ch


class TestTelegramChannel:
    """Tests for TelegramChannel methods."""

    @pytest.mark.asyncio
    async def test_send_message_basic(self, channel: TelegramChannel, mock_bot: AsyncMock) -> None:
        user = User(id="123", telegram_id=123, department="it", name="Test User")
        
        await channel.send_message(user, "Hello World")
        
        mock_bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text="Hello World\n",
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

    @pytest.mark.asyncio
    async def test_send_message_splits_long_text(self, channel: TelegramChannel, mock_bot: AsyncMock) -> None:
        user = User(id="123", telegram_id=123, department="it", name="Test User")
        
        # Create a message long enough to be split (Telegram limit is 4096)
        # We will make it 5000 chars of 'A'
        long_message = "A" * 5000
        
        await channel.send_message(user, long_message)
        
        # Should be called twice
        assert mock_bot.send_message.await_count == 2
        
        # Check first call text
        call_args_1 = mock_bot.send_message.await_args_list[0].kwargs
        assert len(call_args_1["text"]) <= 4096
        
        # Check second call text
        call_args_2 = mock_bot.send_message.await_args_list[1].kwargs
        assert len(call_args_2["text"]) > 0

    @pytest.mark.asyncio
    async def test_request_approval(self, channel: TelegramChannel, mock_bot: AsyncMock) -> None:
        user = User(id="123", telegram_id=123, department="it", name="Test User")
        
        # Mock send_message to return a dummy message with message_id=456
        mock_msg = MagicMock()
        mock_msg.message_id = 456
        mock_bot.send_message.return_value = mock_msg
        
        # Create a task for request_approval
        approval_task = asyncio.create_task(
            channel.request_approval(user, "Test Action", "Details here")
        )
        
        # Yield to allow request_approval to set up the Future
        await asyncio.sleep(0.01)
        
        # The key should be "456" in pending_approvals
        key = "456"
        assert key in channel._pending_approvals
        
        # Resolve the future as True (Approved)
        future, expected_tid = channel._pending_approvals[key]
        assert expected_tid == 123
        future.set_result(True)
        
        result = await approval_task
        assert result is True

    @pytest.mark.asyncio
    async def test_request_approval_deny(self, channel: TelegramChannel, mock_bot: AsyncMock) -> None:
        user = User(id="123", telegram_id=123, department="it", name="Test User")
        
        mock_msg = MagicMock()
        mock_msg.message_id = 456
        mock_bot.send_message.return_value = mock_msg
        
        approval_task = asyncio.create_task(
            channel.request_approval(user, "Delete Database", "-")
        )
        
        await asyncio.sleep(0.01)
        
        key = "456"
        future, _ = channel._pending_approvals[key]
        future.set_result(False) # Denied
        
        result = await approval_task
        assert result is False

    @pytest.mark.asyncio
    async def test_get_user_workspace(self, channel: TelegramChannel, tmp_path: Path) -> None:
        channel._workspace_base = tmp_path
        user = User(id="999", telegram_id=999, department="it", name="Test User")
        
        ws = channel.get_user_workspace(user)
        assert ws.name == "user_999"
        assert ws.exists()
        assert ws.is_dir()
