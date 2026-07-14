"""B-102: channel-scoped virtual sessions (web vs telegram)."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.channels.web.chat_store import (
    CHANNEL_TELEGRAM,
    CHANNEL_WEB,
    WebChatStore,
)


@pytest.mark.asyncio
async def test_telegram_session_does_not_end_web_session(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "memory.db")
    user_id = "user-1"

    web_id = await store.ensure_active_session(user_id, channel=CHANNEL_WEB)
    tg_id = await store.ensure_channel_session(user_id, channel=CHANNEL_TELEGRAM)

    assert web_id != tg_id
    # Both remain open: re-ensure returns same ids.
    assert await store.ensure_active_session(user_id, channel=CHANNEL_WEB) == web_id
    assert await store.ensure_channel_session(user_id, channel=CHANNEL_TELEGRAM) == tg_id


@pytest.mark.asyncio
async def test_ensure_channel_session_idempotent(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "memory.db")
    a = await store.ensure_channel_session("u", channel=CHANNEL_TELEGRAM)
    b = await store.ensure_channel_session("u", channel=CHANNEL_TELEGRAM)
    assert a == b


@pytest.mark.asyncio
async def test_reset_channel_session_only_telegram(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "memory.db")
    user_id = "user-2"
    web_id = await store.ensure_active_session(user_id, channel=CHANNEL_WEB)
    tg_old = await store.ensure_channel_session(user_id, channel=CHANNEL_TELEGRAM)

    tg_new = await store.reset_channel_session(
        user_id, channel=CHANNEL_TELEGRAM, reason="telegram_/new"
    )
    assert tg_new != tg_old
    # Web open session unchanged.
    assert await store.ensure_active_session(user_id, channel=CHANNEL_WEB) == web_id
    # Telegram uses the new session.
    assert await store.ensure_channel_session(user_id, channel=CHANNEL_TELEGRAM) == tg_new


@pytest.mark.asyncio
async def test_web_create_session_does_not_end_telegram(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "memory.db")
    user_id = "user-3"
    tg_id = await store.ensure_channel_session(user_id, channel=CHANNEL_TELEGRAM)
    web_a = await store.create_session(user_id, section="chat")
    web_b = await store.create_session(user_id, section="work")
    assert web_a != web_b
    assert await store.ensure_channel_session(user_id, channel=CHANNEL_TELEGRAM) == tg_id
