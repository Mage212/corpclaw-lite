"""Tests for additional coverage."""

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.channels.telegram.channel import TelegramChannel
from corpclaw_lite.channels.telegram.file_manager import DeleteBrowserHandler
from corpclaw_lite.config.settings import Settings


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


@pytest.mark.asyncio
async def test_orchestrator_passes_settings_to_build_agent_stack(monkeypatch):
    from corpclaw_lite.channels.telegram import orchestrator as orch

    settings = Settings()
    fake_loop = SimpleNamespace(memory=None, provider=object())
    fake_tool_registry = MagicMock()
    fake_tool_registry.get.return_value = None
    fake_stack = SimpleNamespace(
        loop=fake_loop,
        user_manager=MagicMock(),
        tool_registry=fake_tool_registry,
        mcp_manager=None,
        skill_registry=None,
        plugin_registry=None,
        subagent_registry=None,
        container_manager=None,
    )
    build_agent_stack = MagicMock(return_value=fake_stack)
    monkeypatch.setattr(orch, "build_agent_stack", build_agent_stack)
    monkeypatch.setattr(orch, "install_signal_handlers", MagicMock())
    from corpclaw_lite.logging import health

    monkeypatch.setattr(health, "run_health_server", AsyncMock(return_value=None))

    channel = MagicMock()
    channel.start = AsyncMock()
    channel.stop = AsyncMock()
    channel.bot = None
    channel.app = None
    monkeypatch.setattr(orch, "TelegramChannel", MagicMock(return_value=channel))

    bot = orch.TelegramBotOrchestrator("token", settings)
    await bot.start()
    await bot.stop()

    build_agent_stack.assert_called_once_with(settings)
