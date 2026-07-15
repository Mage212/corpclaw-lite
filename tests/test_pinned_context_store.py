"""Tests for B-095 PinnedContextStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.channels.web.chat_store import WebChatStore
from corpclaw_lite.channels.web.pinned_context_store import (
    PinnedContextRecord,
    PinnedContextStore,
    format_pinned_files_block,
)


@pytest.mark.asyncio
async def test_pin_upsert_list_remove(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    chat = WebChatStore(db)
    store = PinnedContextStore(db)
    sid = await chat.ensure_active_session("user-1")
    rec = PinnedContextRecord(
        session_id=sid,
        user_id="user-1",
        path="a.md",
        kind="text",
        tokens=100,
        approximate=True,
        mode="full",
        content="hello",
        label="a.md",
    )
    await store.upsert_pin(rec)
    pins = await store.list_pins(sid, "user-1")
    assert len(pins) == 1
    assert pins[0].content == "hello"
    assert await store.total_tokens(sid, "user-1") == 100

    rec2 = PinnedContextRecord(
        session_id=sid,
        user_id="user-1",
        path="a.md",
        kind="text",
        tokens=50,
        approximate=True,
        mode="chunked",
        content="hi",
        label="a.md",
    )
    await store.upsert_pin(rec2)
    assert await store.total_tokens(sid, "user-1") == 50

    assert await store.remove_pin(sid, "user-1", "a.md") is True
    assert await store.list_pins(sid, "user-1") == []


@pytest.mark.asyncio
async def test_format_pinned_block() -> None:
    pins = [
        PinnedContextRecord(
            session_id=1,
            user_id="u",
            path="a.md",
            kind="text",
            tokens=10,
            approximate=True,
            mode="full",
            content="X",
            label="a.md",
        )
    ]
    block = format_pinned_files_block(pins)
    assert "## Pinned Files" in block
    assert "a.md" in block
    assert "X" in block


@pytest.mark.asyncio
async def test_clear_session(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    chat = WebChatStore(db)
    store = PinnedContextStore(db)
    sid = await chat.ensure_active_session("user-1")
    await store.upsert_pin(
        PinnedContextRecord(
            session_id=sid,
            user_id="user-1",
            path="a.md",
            kind="text",
            tokens=10,
            approximate=True,
            mode="full",
            content="X",
            label="a.md",
        )
    )
    n = await store.clear_session(sid, "user-1")
    assert n == 1
    assert await store.list_pins(sid, "user-1") == []
