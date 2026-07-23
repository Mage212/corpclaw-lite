"""B-120 / DC-032: UserNotifier proactive-send + system session."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.channels.service import AgentRequestService, HeadlessResult
from corpclaw_lite.channels.user_notifier import UserNotifier
from corpclaw_lite.channels.web.chat_store import SECTION_SYSTEM, WebChatStore
from corpclaw_lite.users.models import User


@pytest.mark.asyncio
async def test_notify_persists_to_system_session(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "mem.db")
    user = User(id=1, name="A", department="engineering")
    notifier = UserNotifier(store)

    result = await notifier.notify(user, "Hello proactive", source="test", title="Ping")

    assert result.ok is True
    assert result.session_id is not None
    assert result.message_id is not None
    assert result.web_pushed is False
    assert result.telegram_sent is False

    page = await store.list_messages(user.memory_key(), session_id=result.session_id, limit=10)
    assert len(page.messages) == 1
    msg = page.messages[0]
    assert msg.role == "assistant"
    assert msg.content == "Hello proactive"
    assert msg.metadata.get("proactive") is True
    assert msg.metadata.get("source") == "test"
    assert msg.metadata.get("title") == "Ping"

    summary = await store.get_session(user.memory_key(), result.session_id)
    assert summary is not None
    assert summary.section == SECTION_SYSTEM


@pytest.mark.asyncio
async def test_notify_empty_text_rejected(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "mem.db")
    user = User(id=2, name="B", department="engineering")
    notifier = UserNotifier(store)
    result = await notifier.notify(user, "   ")
    assert result.ok is False
    assert "empty_text" in result.errors


@pytest.mark.asyncio
async def test_notify_extra_metadata_merged_and_protected(tmp_path: Path) -> None:
    """B-143: extra_metadata lands in store + WS; proactive/source cannot be overridden."""
    store = WebChatStore(tmp_path / "mem.db")
    user = User(id=22, name="Meta", department="engineering")
    notifier = UserNotifier(store)
    broadcasts: list[dict[str, object]] = []

    async def _broadcast(_uid: int, payload: dict[str, object]) -> None:
        broadcasts.append(payload)

    notifier.register_web_broadcast(_broadcast)
    result = await notifier.notify(
        user,
        "card body",
        source="schedule_propose",
        title="Подтвердите",
        extra_metadata={
            "kind": "schedule_confirm",
            "task_id": "abc123",
            "source": "hijack",
            "proactive": False,
        },
    )
    assert result.ok is True
    page = await store.list_messages(user.memory_key(), session_id=result.session_id, limit=5)
    assert page.messages[0].metadata.get("kind") == "schedule_confirm"
    assert page.messages[0].metadata.get("task_id") == "abc123"
    assert page.messages[0].metadata.get("source") == "schedule_propose"
    assert page.messages[0].metadata.get("proactive") is True

    proactive = next(p for p in broadcasts if p.get("type") == "proactive_message")
    message = proactive.get("message")
    assert isinstance(message, dict)
    meta = message.get("metadata")
    assert isinstance(meta, dict)
    assert meta.get("task_id") == "abc123"
    assert meta.get("source") == "schedule_propose"


@pytest.mark.asyncio
async def test_notify_web_sink(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "mem.db")
    user = User(id=3, name="C", department="engineering")
    notifier = UserNotifier(store)
    broadcasts: list[tuple[int, dict[str, object]]] = []

    async def _broadcast(uid: int, payload: dict[str, object]) -> None:
        broadcasts.append((uid, payload))

    notifier.register_web_broadcast(_broadcast)
    result = await notifier.notify(user, "WS ping", source="scheduled")

    assert result.ok is True
    assert result.web_pushed is True
    types = [p.get("type") for _, p in broadcasts]
    assert "proactive_message" in types
    assert "chat_list_changed" in types
    proactive = next(p for _, p in broadcasts if p.get("type") == "proactive_message")
    assert proactive.get("session_id") == result.session_id
    assert proactive.get("source") == "scheduled"
    message = proactive.get("message")
    assert isinstance(message, dict)
    assert message.get("text") == "WS ping"


@pytest.mark.asyncio
async def test_notify_telegram_sink_when_telegram_id(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "mem.db")
    user = User(id=4, name="D", department="engineering", telegram_id=999001)
    notifier = UserNotifier(store)
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    notifier.register_telegram_bot(bot)

    result = await notifier.notify(user, "TG hi", source="manual")

    assert result.ok is True
    assert result.telegram_sent is True
    bot.send_message.assert_awaited_once_with(chat_id=999001, text="TG hi")


@pytest.mark.asyncio
async def test_notify_no_telegram_id_skips_tg(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "mem.db")
    user = User(id=5, name="E", department="engineering", telegram_id=None)
    notifier = UserNotifier(store)
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    notifier.register_telegram_bot(bot)

    result = await notifier.notify(user, "no tg", source="manual")

    assert result.ok is True
    assert result.telegram_sent is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_web_and_telegram_both_fire(tmp_path: Path) -> None:
    """F6: D-084 parallel multichannel — both sinks receive when registered."""
    store = WebChatStore(tmp_path / "mem.db")
    user = User(id=15, name="Both", department="engineering", telegram_id=42)
    notifier = UserNotifier(store)
    broadcasts: list[str] = []

    async def _broadcast(uid: int, payload: dict[str, object]) -> None:
        t = payload.get("type")
        if isinstance(t, str):
            broadcasts.append(t)

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    notifier.register_web_broadcast(_broadcast)
    notifier.register_telegram_bot(bot)

    result = await notifier.notify(user, "to both", source="scheduled")

    assert result.ok is True
    assert result.web_pushed is True
    assert result.telegram_sent is True
    assert "proactive_message" in broadcasts
    bot.send_message.assert_awaited_once_with(chat_id=42, text="to both")


@pytest.mark.asyncio
async def test_notify_persist_false_no_double_append(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "mem.db")
    user = User(id=6, name="F", department="engineering")
    session_id = await store.ensure_system_session(user.memory_key())
    await store.append_message(
        user_id=user.memory_key(),
        role="assistant",
        content="already written",
        session_id=session_id,
        metadata={"headless": True, "source": "test"},
    )

    notifier = UserNotifier(store)
    broadcasts: list[dict[str, object]] = []

    async def _broadcast(uid: int, payload: dict[str, object]) -> None:
        broadcasts.append(payload)

    notifier.register_web_broadcast(_broadcast)
    result = await notifier.notify(user, "already written", source="headless:test", persist=False)

    assert result.ok is True
    assert result.message_id is None
    page = await store.list_messages(user.memory_key(), session_id=session_id, limit=20)
    assert len(page.messages) == 1
    assert any(p.get("type") == "proactive_message" for p in broadcasts)


@pytest.mark.asyncio
async def test_list_sessions_section_system(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "mem.db")
    await store.create_session("u", section="chat")
    sys_id = await store.ensure_system_session("u")
    systems = await store.list_sessions("u", section=SECTION_SYSTEM)
    assert len(systems) == 1
    assert systems[0].id == sys_id


@pytest.mark.asyncio
async def test_headless_notify_persist_false_single_assistant(tmp_path: Path) -> None:
    from corpclaw_lite.agent.factory import AgentStack
    from corpclaw_lite.agent.loop import RunStats
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.users.manager import UserManager

    user = User(id=8, name="H", department="engineering")
    store = WebChatStore(tmp_path / "m.db")

    class _FakeLoop:
        async def run(self, *args: Any, **kwargs: Any) -> tuple[str, RunStats]:
            return "done-reply", RunStats(status="ok")

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
    notifier = UserNotifier(store)
    notify_spy = AsyncMock(wraps=notifier.notify)
    notifier.notify = notify_spy  # type: ignore[method-assign]

    service = AgentRequestService(
        stack=stack, workspace_base=tmp_path / "ws", user_notifier=notifier
    )

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

    result = await service.run_headless(user=user, task="do work", source="test")
    assert isinstance(result, HeadlessResult)
    assert result.status == "completed"
    assert result.session_id is not None

    notify_spy.assert_awaited()
    call_kwargs = notify_spy.await_args.kwargs
    assert call_kwargs.get("persist") is False
    assert call_kwargs.get("source") == "headless:test"

    page = await store.list_messages(user.memory_key(), session_id=result.session_id, limit=20)
    assistants = [m for m in page.messages if m.role == "assistant"]
    assert len(assistants) == 1
    assert assistants[0].content == "done-reply"
