"""B-143 PR2: schedule consent Telegram markup + callback parsing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.channels.telegram.callback_data import (
    parse_schedule_callback,
    schedule_accept_data,
    schedule_dismiss_data,
)
from corpclaw_lite.channels.telegram.schedule_markup import (
    build_schedule_confirm_markup,
    markup_from_notify_metadata,
)
from corpclaw_lite.channels.user_notifier import UserNotifier
from corpclaw_lite.channels.web.chat_store import WebChatStore
from corpclaw_lite.users.models import User


def test_schedule_callback_roundtrip() -> None:
    task_id = "a" * 32
    accept = schedule_accept_data(task_id)
    dismiss = schedule_dismiss_data(task_id)
    assert len(accept) <= 64
    assert len(dismiss) <= 64
    assert parse_schedule_callback(accept) == ("accept", task_id)
    assert parse_schedule_callback(dismiss) == ("dismiss", task_id)
    assert parse_schedule_callback("sc:x:nope") is None
    assert parse_schedule_callback("del:file:0") is None


def test_markup_from_metadata() -> None:
    task_id = "b" * 32
    mk = markup_from_notify_metadata(
        {
            "kind": "schedule_confirm",
            "task_id": task_id,
            "status": "pending",
        }
    )
    assert mk is not None
    rows = mk.inline_keyboard
    assert len(rows) == 1
    assert len(rows[0]) == 2
    assert rows[0][0].callback_data == schedule_accept_data(task_id)
    assert rows[0][1].callback_data == schedule_dismiss_data(task_id)

    assert markup_from_notify_metadata({"kind": "other", "task_id": task_id}) is None
    assert (
        markup_from_notify_metadata(
            {"kind": "schedule_confirm", "task_id": task_id, "status": "active"}
        )
        is None
    )


def test_build_markup_buttons() -> None:
    mk = build_schedule_confirm_markup("c" * 32)
    assert mk.inline_keyboard[0][0].text.startswith("✅")
    assert mk.inline_keyboard[0][1].text.startswith("❌")


@pytest.mark.asyncio
async def test_notify_sends_reply_markup_for_schedule_confirm(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "m.db")
    user = User(id=9, name="TG", department="engineering", telegram_id=111)
    notifier = UserNotifier(store)
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    notifier.register_telegram_bot(bot)
    notifier.register_telegram_markup_builder(markup_from_notify_metadata)

    task_id = "d" * 32
    result = await notifier.notify(
        user,
        "Предложена задача",
        source="schedule_propose",
        extra_metadata={
            "kind": "schedule_confirm",
            "task_id": task_id,
            "status": "pending",
            "title": "T",
        },
    )
    assert result.ok is True
    assert result.telegram_sent is True
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs.get("chat_id") == 111
    assert kwargs.get("text") == "Предложена задача"
    markup = kwargs.get("reply_markup")
    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == schedule_accept_data(task_id)


@pytest.mark.asyncio
async def test_notify_plain_without_schedule_meta_no_markup(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "m.db")
    user = User(id=10, name="TG2", department="engineering", telegram_id=222)
    notifier = UserNotifier(store)
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    notifier.register_telegram_bot(bot)
    notifier.register_telegram_markup_builder(markup_from_notify_metadata)

    await notifier.notify(user, "plain", source="manual")
    kwargs = bot.send_message.await_args.kwargs
    assert "reply_markup" not in kwargs or kwargs.get("reply_markup") is None
