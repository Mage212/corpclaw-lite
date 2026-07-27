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

    # Update run — a completed run advances last_run_at (S1-11)
    um.update_memory_worker_run(1, status="ok", advance_last_run_at=True)
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


# ── PR2: MemoryWorkerService integration tests ────────────────────────────

from typing import Any  # noqa: E402

from corpclaw_lite.llm.base import LLMResponse  # noqa: E402
from corpclaw_lite.memory.worker import MemoryWorkerService  # noqa: E402


def _mw_settings(tmp_path: Path, **overrides: Any) -> Any:
    from corpclaw_lite.config.settings import MemoryWorkerSettings

    defaults: dict[str, Any] = {
        "enabled": True,
        "interval_hours": 24.0,
        "quiet_hours_start": "00:00",
        "quiet_hours_end": "23:59",
        "timezone": "UTC",
        "max_users_per_tick": 20,
        "max_sessions": 5,
        "max_messages_per_session": 40,
        "max_transcript_chars": 24_000,
        "max_entries_per_run": 20,
        "notify_on_update": False,
        "keep_history_backups": False,
        "bootstrap_users_dir": "config/bootstrap/users",
        "entries_backup_dir": "memory_backups",
        "poll_seconds": 300.0,
    }
    defaults.update(overrides)
    return MemoryWorkerSettings(**defaults)


class _FakeProvider:
    """Minimal provider for worker tests — returns canned JSON."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content=self._content, tool_calls=[])


class _FakeAgentService:
    """Fake gate that always allows (or denies)."""

    def __init__(self, *, allow: bool = True) -> None:
        self._allow = allow

    async def try_start_user_request(self, *args: Any, **kwargs: Any) -> bool:
        return self._allow

    async def finish_user_request(self, *args: Any, **kwargs: Any) -> Any:
        return None


def _build_worker(
    tmp_path: Path,
    *,
    provider_content: str = "",
    allow_gate: bool = True,
    settings_overrides: dict[str, Any] | None = None,
) -> tuple[MemoryWorkerService, UserManager, SQLiteMemory, Path]:
    """Construct a MemoryWorkerService with fakes for integration testing."""
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore
    from corpclaw_lite.config.bootstrap import BootstrapLoader

    db = tmp_path / "mem.db"
    users_db = tmp_path / "users.db"
    um = UserManager(str(users_db))
    memory = SQLiteMemory(str(db))
    chat_store = WebChatStore(db)
    ctx_store = ChatContextStore(db)
    bootstrap = BootstrapLoader(tmp_path / "bootstrap")
    (tmp_path / "bootstrap" / "users").mkdir(parents=True, exist_ok=True)

    settings = _mw_settings(tmp_path, **(settings_overrides or {}))
    provider = _FakeProvider(provider_content)
    worker = MemoryWorkerService(
        settings=settings,
        user_manager=um,
        memory=memory,
        chat_store=chat_store,
        context_store=ctx_store,
        bootstrap=bootstrap,
        provider=provider,
        agent_service=_FakeAgentService(allow=allow_gate),
    )
    return worker, um, memory, tmp_path


@pytest.mark.asyncio
async def test_run_user_cold_start_skip(tmp_path: Path) -> None:
    """No transcript + no md → skip, no LLM call."""
    from corpclaw_lite.users.models import User

    worker, um, _, _ = _build_worker(tmp_path, provider_content="SHOULD NOT BE CALLED")
    user = User(id=1, name="Cold", department="engineering")
    status = await worker.run_user(user)
    assert status == "skipped"
    state = um.get_memory_worker_state(1)
    assert state is not None
    assert state.last_status == "skipped"


@pytest.mark.asyncio
async def test_run_user_busy_skip(tmp_path: Path) -> None:
    """Busy gate denies → skip, no LLM call."""
    from corpclaw_lite.users.models import User

    worker, um, _, _ = _build_worker(
        tmp_path, provider_content="SHOULD NOT BE CALLED", allow_gate=False
    )
    user = User(id=2, name="Busy", department="engineering")
    status = await worker.run_user(user)
    assert status == "skipped"
    state = um.get_memory_worker_state(2)
    assert state is not None
    assert "busy" in (state.last_error or "")


@pytest.mark.asyncio
async def test_run_user_success_fake_llm(tmp_path: Path) -> None:
    """Valid JSON → md written, .bak exists, entries upserted, existing survives."""
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore
    from corpclaw_lite.users.models import User

    llm_json = json.dumps(
        {
            "md": "# About\n\nUpdated profile.",
            "entries": [
                {"abstraction": "prefers ru", "value": "Russian speaker", "cues": ["ru"]},
            ],
            "summary": "Added language preference",
        }
    )

    worker, um, memory, base = _build_worker(tmp_path, provider_content=llm_json)
    user = User(id=3, name="Success", department="engineering")

    # Pre-existing entry that should survive merge-only.
    await memory.store_entry("3", primary_abstraction="old_fact", memory_value="stays", cues=[])

    # Create a chat session with transcript so it's not cold-start.
    chat_store = WebChatStore(base / "mem.db")
    ctx_store = ChatContextStore(base / "mem.db")
    sid = await chat_store.create_session("3", section="work")
    await ctx_store.append_context(session_id=sid, user_id="3", role="user", content="hello")

    status = await worker.run_user(user)
    assert status == "ok"

    state = um.get_memory_worker_state(3)
    assert state is not None
    assert state.last_status == "ok"

    # New entry stored + old entry survived.
    recalled = await memory.recall_entries("3", limit=20)
    abstractions = {e["abstraction"] for e in recalled}
    assert "prefers ru" in abstractions
    assert "old_fact" in abstractions  # merge-only

    # .md written.
    md_path = base / "bootstrap" / "users" / "3.md"
    assert md_path.exists()
    content = md_path.read_text(encoding="utf-8")
    assert "Updated profile" in content
    assert "auto-managed" in content  # disclaimer ensured

    # Backup exists only when there was a prior .md to back up. On first write
    # there is no prior file, so no .bak — that's correct. Test backup separately
    # in the unit tests above (test_backup_creates_bak_with_previous_content).


@pytest.mark.asyncio
async def test_run_user_bad_json_no_write(tmp_path: Path) -> None:
    """Prose response → error, no file mutation."""
    from corpclaw_lite.users.models import User

    worker, um, _, base = _build_worker(tmp_path, provider_content="This is prose, not JSON.")
    user = User(id=4, name="Bad", department="engineering")

    # Pre-create a .md to verify it's NOT overwritten on error.
    md_path = base / "bootstrap" / "users" / "4.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("ORIGINAL", encoding="utf-8")

    # Give it a transcript so it's not cold-start.
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore

    chat_store = WebChatStore(base / "mem.db")
    ctx_store = ChatContextStore(base / "mem.db")
    sid = await chat_store.create_session("4", section="work")
    await ctx_store.append_context(session_id=sid, user_id="4", role="user", content="hi")

    status = await worker.run_user(user)
    assert status == "error"
    state = um.get_memory_worker_state(4)
    assert state is not None
    assert state.last_status == "error"
    # File untouched.
    assert md_path.read_text(encoding="utf-8") == "ORIGINAL"


@pytest.mark.asyncio
async def test_run_user_no_provider_error(tmp_path: Path) -> None:
    """No provider configured → error."""
    from corpclaw_lite.users.models import User

    worker, um, memory, base = _build_worker(tmp_path, provider_content="")
    worker._provider = None  # type: ignore[attr-defined]
    user = User(id=5, name="NoProv", department="engineering")

    # Give transcript to pass cold-start check.
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore

    chat_store = WebChatStore(base / "mem.db")
    ctx_store = ChatContextStore(base / "mem.db")
    sid = await chat_store.create_session("5", section="work")
    await ctx_store.append_context(session_id=sid, user_id="5", role="user", content="hi")

    status = await worker.run_user(user)
    assert status == "error"


def test_router_memory_worker_to_maintenance() -> None:
    """_load_class_for_task maps memory_worker → maintenance (overflow)."""
    from corpclaw_lite.llm.router import _load_class_for_task

    assert _load_class_for_task("memory_worker") == "maintenance"


def test_quiet_hours_wrap_midnight() -> None:
    """22:00–07:00 window: 23:00 is quiet, 12:00 is not."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    worker, _, _, _ = _build_worker(
        Path("/tmp"),  # not used for this test
        settings_overrides={"quiet_hours_start": "22:00", "quiet_hours_end": "07:00"},
    )
    tz = ZoneInfo("UTC")
    assert worker._is_quiet_hours(now=datetime(2026, 1, 1, 23, 0, tzinfo=tz)) is True
    assert worker._is_quiet_hours(now=datetime(2026, 1, 1, 12, 0, tzinfo=tz)) is False
    assert worker._is_quiet_hours(now=datetime(2026, 1, 1, 6, 0, tzinfo=tz)) is True  # before 07:00
    assert worker._is_quiet_hours(now=datetime(2026, 1, 1, 22, 0, tzinfo=tz)) is True  # at start


def test_quiet_hours_same_day() -> None:
    """09:00–17:00 window: 12:00 is quiet, 20:00 is not."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    worker, _, _, _ = _build_worker(
        Path("/tmp"),
        settings_overrides={"quiet_hours_start": "09:00", "quiet_hours_end": "17:00"},
    )
    tz = ZoneInfo("UTC")
    assert worker._is_quiet_hours(now=datetime(2026, 1, 1, 12, 0, tzinfo=tz)) is True
    assert worker._is_quiet_hours(now=datetime(2026, 1, 1, 20, 0, tzinfo=tz)) is False


# ── S1-11: scheduler starvation + error propagation ────────────────────────


def test_skip_does_not_advance_last_run_at(tmp_path: Path) -> None:
    """S1-11: a skipped/error run must NOT bump last_run_at.

    Otherwise a consistently-busy user (always skipped at poll time) gets a
    fresh timestamp every tick and is demoted to the back of the run queue
    forever — starving it.
    """
    um = UserManager(str(tmp_path / "users.db"))
    um.set_memory_worker_enabled(1, True)
    # A genuine completed run advances last_run_at.
    um.update_memory_worker_run(1, status="ok", advance_last_run_at=True)
    after_ok = um.get_memory_worker_state(1)
    assert after_ok is not None and after_ok.last_run_at is not None
    ok_ts = after_ok.last_run_at

    # A skipped run records status but does NOT change last_run_at.
    um.update_memory_worker_run(1, status="skipped", error="user_busy")
    after_skip = um.get_memory_worker_state(1)
    assert after_skip is not None
    assert after_skip.last_status == "skipped"
    assert after_skip.last_error == "user_busy"
    assert after_skip.last_run_at == ok_ts  # unchanged

    # An error run likewise does not advance last_run_at.
    um.update_memory_worker_run(1, status="error", error="boom")
    after_err = um.get_memory_worker_state(1)
    assert after_err is not None
    assert after_err.last_status == "error"
    assert after_err.last_run_at == ok_ts  # unchanged


def test_set_memory_worker_enabled_propagates_db_error(tmp_path: Path) -> None:
    """S1-11: set_memory_worker_enabled raises on DB failure (no silent swallow)."""
    import sqlite3
    from unittest.mock import patch

    um = UserManager(str(tmp_path / "users.db"))
    err = sqlite3.OperationalError("locked")
    with (
        patch("corpclaw_lite.users.manager.db_connect", side_effect=err),
        pytest.raises(sqlite3.OperationalError),
    ):
        um.set_memory_worker_enabled(1, True)


def test_set_agent_context_propagates_db_error(tmp_path: Path) -> None:
    """S1-11: set_agent_context raises on DB failure (no silent swallow)."""
    import sqlite3
    from unittest.mock import patch

    um = UserManager(str(tmp_path / "users.db"))
    err = sqlite3.OperationalError("disk full")
    with (
        patch("corpclaw_lite.users.manager.db_connect", side_effect=err),
        pytest.raises(sqlite3.OperationalError),
    ):
        um.set_agent_context(1, instructions="x", tone="default")
