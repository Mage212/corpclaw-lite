"""B-109 PR1: unit tests for memory-worker merge/transcript helpers + storage CRUD."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpclaw_lite.memory.sqlite import SQLiteMemory
from corpclaw_lite.memory.worker_merge import (
    WorkerEntry,
    apply_worker_entries,
    backup_user_md,
    ensure_disclaimer,
    gather_transcript,
    parse_worker_response,
    write_user_md_atomic,
)
from corpclaw_lite.users.manager import UserManager

# ── .md helpers ───────────────────────────────────────────────────────────


def test_backup_creates_bak_with_previous_content(tmp_path: Path) -> None:
    md = tmp_path / "1.md"
    md.write_text("old content", encoding="utf-8")
    bak = backup_user_md(md)
    assert bak is not None and bak.exists()
    md.write_text("new content", encoding="utf-8")
    assert bak.read_text(encoding="utf-8") == "old content"


def test_backup_returns_none_when_missing(tmp_path: Path) -> None:
    assert backup_user_md(tmp_path / "absent.md") is None


def test_backup_keep_history_creates_timestamped(tmp_path: Path) -> None:
    md = tmp_path / "2.md"
    md.write_text("v1", encoding="utf-8")
    backup_user_md(md, keep_history=True)
    history = [p for p in tmp_path.iterdir() if ".bak." in p.name]
    assert len(history) >= 1


def test_write_user_md_atomic(tmp_path: Path) -> None:
    md = tmp_path / "sub" / "3.md"
    write_user_md_atomic(md, "hello\nworld")
    assert md.read_text(encoding="utf-8") == "hello\nworld"
    # No leftover temp files
    temps = [p for p in (tmp_path / "sub").iterdir() if p.name.endswith(".tmp")]
    assert temps == []


def test_ensure_disclaimer_prepends_header() -> None:
    without = "# About\n\nLikes concise answers."
    result = ensure_disclaimer(without)
    assert "auto-managed" in result
    assert without in result
    # Idempotent
    assert ensure_disclaimer(result) == result


# ── LLM response parsing ──────────────────────────────────────────────────


def test_parse_worker_response_valid() -> None:
    raw = json.dumps(
        {
            "md": "# About\n\nTest user.",
            "entries": [
                {
                    "abstraction": "prefers concise",
                    "value": "User likes short answers",
                    "cues": ["concise"],
                },
            ],
            "summary": "Added preference",
        }
    )
    update = parse_worker_response(raw)
    assert update is not None
    assert "Test user" in update.md
    assert len(update.entries) == 1
    assert update.entries[0].abstraction == "prefers concise"
    assert update.summary == "Added preference"


def test_parse_worker_response_tolerates_fences() -> None:
    raw = (
        "```json\n"
        + json.dumps(
            {
                "md": "body",
                "entries": [],
                "summary": "",
            }
        )
        + "\n```"
    )
    update = parse_worker_response(raw)
    assert update is not None
    assert update.md == "body"


def test_parse_worker_response_rejects_prose() -> None:
    assert parse_worker_response("This is not JSON.") is None


def test_parse_worker_response_rejects_missing_md() -> None:
    assert parse_worker_response(json.dumps({"entries": [], "summary": ""})) is None


def test_parse_worker_response_caps_entries() -> None:
    entries = [{"abstraction": f"a{i}", "value": f"v{i}"} for i in range(30)]
    raw = json.dumps({"md": "body", "entries": entries, "summary": ""})
    update = parse_worker_response(raw, max_entries=20)
    assert update is not None
    assert len(update.entries) == 20


def test_parse_worker_response_rejects_bad_entry_types() -> None:
    raw = json.dumps(
        {
            "md": "body",
            "entries": [
                {"abstraction": 123, "value": "ok"},  # bad abstraction type
                {"abstraction": "good", "value": "ok"},
            ],
            "summary": "",
        }
    )
    update = parse_worker_response(raw)
    assert update is not None
    assert len(update.entries) == 1
    assert update.entries[0].abstraction == "good"


# ── Entry application (merge-only) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_entries_upserts(tmp_path: Path) -> None:
    mem = SQLiteMemory(str(tmp_path / "mem.db"))
    entries = [
        WorkerEntry(abstraction="prefers_ru", value="Russian speaker", cues=["ru"]),
        WorkerEntry(abstraction="role", value="Engineer", cues=["eng"]),
    ]
    count = await apply_worker_entries(mem, "1", entries)
    assert count == 2
    recalled = await mem.recall_entries("1", limit=10)
    abstractions = {e["abstraction"] for e in recalled}
    assert abstractions == {"prefers_ru", "role"}


@pytest.mark.asyncio
async def test_apply_entries_never_deletes(tmp_path: Path) -> None:
    mem = SQLiteMemory(str(tmp_path / "mem.db"))
    # Pre-existing entry
    await mem.store_entry("1", primary_abstraction="existing", memory_value="old", cues=[])
    # Worker proposes a different entry, does NOT mention "existing"
    await apply_worker_entries(
        mem,
        "1",
        [
            WorkerEntry(abstraction="new", value="fresh", cues=[]),
        ],
    )
    recalled = await mem.recall_entries("1", limit=10)
    abstractions = {e["abstraction"] for e in recalled}
    assert "existing" in abstractions  # merge-only: not deleted
    assert "new" in abstractions


# ── Transcript gather ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_transcript_excludes_system_sessions(tmp_path: Path) -> None:
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore

    db = tmp_path / "mem.db"
    chat_store = WebChatStore(db)
    ctx_store = ChatContextStore(db)
    user_id = "42"

    # Create a work session with messages
    work_id = await chat_store.create_session(user_id, section="work")
    await ctx_store.append_context(
        session_id=work_id, user_id=user_id, role="user", content="hello from work"
    )
    await ctx_store.append_context(
        session_id=work_id, user_id=user_id, role="assistant", content="hi"
    )

    # Create a system session (should be excluded)
    sys_id = await chat_store.ensure_system_session(user_id)
    await ctx_store.append_context(
        session_id=sys_id, user_id=user_id, role="user", content="system noise"
    )

    transcript = await gather_transcript(
        chat_store=chat_store,
        context_store=ctx_store,
        user_id=user_id,
        max_sessions=5,
        max_messages_per_session=40,
        max_chars=10_000,
    )
    assert "hello from work" in transcript
    assert "system noise" not in transcript


@pytest.mark.asyncio
async def test_gather_transcript_drops_tool_roles(tmp_path: Path) -> None:
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore

    db = tmp_path / "mem.db"
    chat_store = WebChatStore(db)
    ctx_store = ChatContextStore(db)
    user_id = "7"

    session_id = await chat_store.create_session(user_id, section="chat")
    await ctx_store.append_context(
        session_id=session_id, user_id=user_id, role="user", content="question"
    )
    await ctx_store.append_context(
        session_id=session_id, user_id=user_id, role="tool", content="tool noise"
    )
    await ctx_store.append_context(
        session_id=session_id, user_id=user_id, role="assistant", content="answer"
    )

    transcript = await gather_transcript(
        chat_store=chat_store,
        context_store=ctx_store,
        user_id=user_id,
        max_sessions=5,
        max_messages_per_session=40,
        max_chars=10_000,
    )
    assert "question" in transcript
    assert "answer" in transcript
    assert "tool noise" not in transcript


# ── UserManager storage CRUD ──────────────────────────────────────────────


def test_user_memory_worker_table_crud(tmp_path: Path) -> None:
    um = UserManager(str(tmp_path / "users.db"))
    # Initially no state
    assert um.get_memory_worker_state(1) is None
    assert um.list_memory_worker_enabled_users() == []

    # Enable
    um.set_memory_worker_enabled(1, True)
    state = um.get_memory_worker_state(1)
    assert state is not None
    assert state.enabled is True
    assert 1 in um.list_memory_worker_enabled_users()

    # Update run
    um.update_memory_worker_run(1, status="ok")
    state = um.get_memory_worker_state(1)
    assert state is not None
    assert state.last_status == "ok"
    assert state.last_run_at is not None

    # Disable
    um.set_memory_worker_enabled(1, False)
    state = um.get_memory_worker_state(1)
    assert state is not None
    assert state.enabled is False
    assert um.list_memory_worker_enabled_users() == []

    # last_run_at persists after disable (we only flip enabled, not wipe history)
    assert state.last_status == "ok"


def test_user_memory_worker_multiple_users(tmp_path: Path) -> None:
    um = UserManager(str(tmp_path / "users.db"))
    um.set_memory_worker_enabled(10, True)
    um.set_memory_worker_enabled(20, True)
    um.set_memory_worker_enabled(30, False)
    enabled = um.list_memory_worker_enabled_users()
    assert set(enabled) == {10, 20}


def test_update_memory_worker_run_truncates_error(tmp_path: Path) -> None:
    um = UserManager(str(tmp_path / "users.db"))
    long_error = "x" * 5000
    um.set_memory_worker_enabled(1, True)
    um.update_memory_worker_run(1, status="error", error=long_error)
    state = um.get_memory_worker_state(1)
    assert state is not None
    assert state.last_status == "error"
    assert len(state.last_error or "") <= 2000


def test_update_memory_worker_run_does_not_auto_enable(tmp_path: Path) -> None:
    """update_memory_worker_run must not auto-enable a non-opted-in user."""
    um = UserManager(str(tmp_path / "users.db"))
    assert 99 not in um.list_memory_worker_enabled_users()
    # Worker records a run for user 99 (e.g. manual CLI run before opt-in).
    um.update_memory_worker_run(99, status="ok")
    state = um.get_memory_worker_state(99)
    assert state is not None
    assert state.enabled is False  # defense-in-depth: no auto-enable
    assert state.last_status == "ok"
    assert 99 not in um.list_memory_worker_enabled_users()
