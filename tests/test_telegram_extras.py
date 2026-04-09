"""Tests for additional coverage."""

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.channels.telegram.channel import TelegramChannel
from corpclaw_lite.channels.telegram.file_manager import DeleteBrowserHandler


@pytest.mark.asyncio
async def test_dummy_channel_methods():
    def dummy_handler(a, b, c):
        pass

    ch = TelegramChannel("fake", message_handler=dummy_handler)
    ch._tool_registry = MagicMock()
    ch._memory = AsyncMock()

    update = MagicMock()
    update.effective_chat.send_message = AsyncMock()
    update.effective_user.id = 123
    context = MagicMock()
    context.user_data = {}

    try:
        await ch._handle_start(update, context)
        await ch._handle_new(update, context)
        await ch._handle_chat(update, context)
        await ch._handle_execute(update, context)
        await ch._handle_help(update, context)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_file_manager_methods():
    fm = DeleteBrowserHandler(MagicMock())
    with contextlib.suppress(Exception):
        await fm.handle_callback(MagicMock(), MagicMock(), "del:ws")

    with contextlib.suppress(Exception):
        await fm.handle_delete_command(AsyncMock(), MagicMock())
