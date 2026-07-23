"""B-119 / DC-031: headless-run + system session."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.agent.loop import RunStats
from corpclaw_lite.channels.service import AgentRequestService, HeadlessResult
from corpclaw_lite.channels.web.chat_store import (
    CHANNEL_WEB,
    SECTION_SYSTEM,
    WebChatStore,
)
from corpclaw_lite.users.models import User


@pytest.mark.asyncio
async def test_ensure_system_session_does_not_end_web(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "mem.db")
    user_id = "u1"
    web_id = await store.create_session(user_id, section="work")
    sys_id = await store.ensure_system_session(user_id)
    assert sys_id != web_id
    # Work session still active
    assert await store.ensure_active_session(user_id, channel=CHANNEL_WEB) == web_id
    # System reuses same id
    assert await store.ensure_system_session(user_id) == sys_id
    summary = await store.get_session(user_id, sys_id)
    assert summary is not None
    assert summary.section == SECTION_SYSTEM


@pytest.mark.asyncio
async def test_list_chat_excludes_system_session(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "mem.db")
    await store.create_session("u", section="chat")
    await store.ensure_system_session("u")
    chats = await store.list_sessions("u", section="chat")
    assert all(s.section == "chat" for s in chats)
    systems = await store.list_sessions("u", section=SECTION_SYSTEM)
    assert len(systems) == 1
    assert systems[0].section == SECTION_SYSTEM


@pytest.mark.asyncio
async def test_run_headless_skips_when_busy(tmp_path: Path) -> None:
    from corpclaw_lite.agent.factory import AgentStack
    from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
    from corpclaw_lite.config.settings import AgentSettings
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.users.manager import UserManager

    class _FakeProvider:
        async def chat(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("should not call LLM when skipped")

    user = User(id=5, name="T", department="engineering")
    store = WebChatStore(tmp_path / "m.db")
    loop = AgentLoop(
        AgentConfig(
            provider=_FakeProvider(),  # type: ignore[arg-type]
            registry=ToolRegistry(),
            settings=AgentSettings(),
        )
    )
    stack = AgentStack(
        loop=loop,
        user_manager=UserManager(),
        tool_registry=ToolRegistry(),
        full_tool_registry=None,
        mcp_manager=None,
        container_manager=None,
        chat_store=store,
        chat_context_store=None,
    )
    service = AgentRequestService(stack=stack, workspace_base=tmp_path / "ws")
    assert await service.try_start_user_request(user.id) is True
    result = await service.run_headless(user=user, task="do something", source="test")
    assert result.status == "skipped"
    assert result.skip_reason == "user_busy"
    await service.finish_user_request(user.id)


@pytest.mark.asyncio
async def test_run_headless_completed_persists_system_transcript(tmp_path: Path) -> None:
    from corpclaw_lite.agent.factory import AgentStack
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.users.manager import UserManager

    user = User(id=7, name="T", department="engineering")
    store = WebChatStore(tmp_path / "m.db")
    work_id = await store.create_session(user.memory_key(), section="work")

    class _FakeLoop:
        async def run(self, *args: Any, **kwargs: Any) -> tuple[str, RunStats]:
            assert kwargs.get("channel") == "system"
            assert kwargs.get("session_id") is not None
            stats = RunStats(status="ok")
            return "done-reply", stats

    stack = AgentStack(
        loop=_FakeLoop(),  # type: ignore[arg-type]
        user_manager=UserManager(),
        tool_registry=ToolRegistry(),
        full_tool_registry=None,
        mcp_manager=None,
        container_manager=None,
        chat_store=store,
        chat_context_store=None,
    )
    service = AgentRequestService(stack=stack, workspace_base=tmp_path / "ws")

    # Patch service.run to avoid full skill/container path — call loop directly shape
    async def _fake_run(**kwargs: Any) -> Any:
        from corpclaw_lite.channels.service import AgentRequestResult

        reply, stats = await stack.loop.run(  # type: ignore[misc]
            kwargs["user"],
            kwargs["message"],
            channel=kwargs.get("channel"),
            session_id=kwargs.get("session_id"),
        )
        return AgentRequestResult(reply=reply, stats=stats)

    service.run = AsyncMock(side_effect=_fake_run)  # type: ignore[method-assign]

    result = await service.run_headless(user=user, task="Summarize sales", source="test")
    assert isinstance(result, HeadlessResult)
    assert result.status == "completed"
    assert result.reply == "done-reply"
    assert result.session_id is not None
    assert result.session_id != work_id

    # Work session still active
    assert await store.ensure_active_session(user.memory_key(), channel=CHANNEL_WEB) == work_id

    page = await store.list_messages(user.memory_key(), session_id=result.session_id, limit=20)
    roles = [m.role for m in page.messages]
    assert "user" in roles and "assistant" in roles
    user_msg = next(m for m in page.messages if m.role == "user")
    assert user_msg.metadata.get("source") == "test"
    assert user_msg.metadata.get("headless") is True


def test_sticky_eligible_excludes_headless_task_kind() -> None:
    from corpclaw_lite.llm.queue import LLMRequestQueue, QueueEntry

    entry = QueueEntry(
        user_id="1",
        task_kind="headless",
        load_class="subagent",
    )
    assert LLMRequestQueue._is_sticky_eligible(entry) is False
    sticky = QueueEntry(user_id="1", task_kind="default", load_class="interactive")
    assert LLMRequestQueue._is_sticky_eligible(sticky) is True
