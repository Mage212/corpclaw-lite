"""B-090 / DC-011: RunningRequest gate (session-aware in-flight lock)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.agent.factory import AgentStack
from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
from corpclaw_lite.channels.service import AgentRequestService, RunningRequest
from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import LLMResponse, Provider
from corpclaw_lite.users.manager import UserManager


def _service(tmp_path: Path) -> AgentRequestService:
    user_manager = UserManager(db_path=str(tmp_path / "users.db"))
    bootstrap = BootstrapLoader(tmp_path / "bootstrap")
    provider = AsyncMock(spec=Provider)
    provider.chat.return_value = LLMResponse(content="ok", tool_calls=[])
    loop = AgentLoop(
        AgentConfig(
            provider=provider,
            registry=ToolRegistry(),
            settings=AgentSettings(),
            default_system_prompt="BASE",
            bootstrap=bootstrap,
            user_manager=user_manager,
        )
    )
    stack = AgentStack(
        loop=loop,
        user_manager=user_manager,
        tool_registry=None,  # type: ignore[arg-type]
        full_tool_registry=None,
        mcp_manager=None,
        container_manager=None,
        skill_registry=None,
        plugin_registry=None,
        skill_matcher=None,
    )
    return AgentRequestService(
        stack=stack,
        bootstrap=bootstrap,
        workspace_base=tmp_path / "ws",
    )


@pytest.mark.asyncio
async def test_try_start_with_session_and_get_running(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    assert await svc.try_start_user_request(1, session_id=42, title="Отчёт") is True
    running = await svc.get_running_request(1)
    assert running == RunningRequest(session_id=42, title="Отчёт")
    assert await svc.active_user_count() == 1
    assert await svc.try_start_user_request(1, session_id=99) is False
    finished = await svc.finish_user_request(1)
    assert finished == RunningRequest(session_id=42, title="Отчёт")
    assert await svc.get_running_request(1) is None
    assert await svc.active_user_count() == 0


@pytest.mark.asyncio
async def test_short_mutex_has_no_session(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    assert await svc.try_start_user_request(7) is True
    running = await svc.get_running_request(7)
    assert running is not None
    assert running.session_id is None
    assert running.title is None
    assert await svc.finish_user_request(7) is not None


@pytest.mark.asyncio
async def test_active_user_count_counts_distinct_users(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    assert await svc.try_start_user_request(1, session_id=1) is True
    assert await svc.try_start_user_request(2, session_id=2) is True
    assert await svc.active_user_count() == 2
    await svc.finish_user_request(1)
    assert await svc.active_user_count() == 1
