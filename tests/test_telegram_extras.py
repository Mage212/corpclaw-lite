"""Tests for additional coverage.

S2-18: the previous blanket-suppress tests (test_dummy_channel_methods,
test_file_manager_methods) were removed — they used `except Exception: pass`
with zero assertions, so they passed unconditionally and gave false coverage
confidence. The handlers they touched are covered by the focused channel
and file-manager test suites.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.config.settings import Settings


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

    build_agent_stack.assert_called_once_with(settings, host_tools_surface="multiuser")
