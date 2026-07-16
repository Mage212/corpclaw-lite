"""B-118 H1–H5 hardening: claim, denylist, dedup, caps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.agent.loop import RunStats
from corpclaw_lite.channels.service import HeadlessResult
from corpclaw_lite.scheduler.service import SchedulerError, SchedulerService
from corpclaw_lite.scheduler.store import SchedulerStore
from corpclaw_lite.users.models import User


class _FakeUM:
    def __init__(self, user: User) -> None:
        self._user = user

    def get_by_id(self, user_id: int) -> User | None:
        return self._user if user_id == self._user.id else None


def _settings(tmp_path: Path) -> Any:
    from corpclaw_lite.config.settings import SchedulerSettings

    return SchedulerSettings(
        enabled=True,
        poll_seconds=30,
        max_tasks_per_user=3,
        timezone="UTC",
        pending_ttl_days=14,
        db_path=str(tmp_path / "scheduler.db"),
        claim_ttl_seconds=900.0,
        max_task_text_chars=100,
        max_schedule_text_chars=50,
    )


@pytest.mark.asyncio
async def test_claim_second_fails(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=10, name="A", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    p = await svc.propose(user, title="t", task_text="x", schedule_text="every 1h", notify=False)
    a = await svc.accept(user, p.id)
    assert a.next_run_at is not None
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    stale = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    ok1 = await store.claim_task(
        a.id,
        expected_next_run_at=a.next_run_at,
        now_iso=now,
        claim_token="tok1",
        stale_before_iso=stale,
    )
    ok2 = await store.claim_task(
        a.id,
        expected_next_run_at=a.next_run_at,
        now_iso=now,
        claim_token="tok2",
        stale_before_iso=stale,
    )
    assert ok1 is True
    assert ok2 is False


@pytest.mark.asyncio
async def test_list_due_skips_fresh_claim(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=11, name="B", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    p = await svc.propose(user, title="t", task_text="x", schedule_text="1m", notify=False)
    a = await svc.accept(user, p.id)
    a.next_run_at = "2000-01-01T00:00:00+00:00"
    await store.update(a)
    now = "2026-07-15T12:00:00+00:00"
    stale = "2026-07-15T11:00:00+00:00"
    await store.claim_task(
        a.id,
        expected_next_run_at=a.next_run_at,
        now_iso=now,
        claim_token="tok",
        stale_before_iso=stale,
    )
    due = await store.list_due(now, stale_before_iso=stale, limit=10)
    assert all(t.id != a.id for t in due)
    # Stale claim is reclaimable
    due2 = await store.list_due(now, stale_before_iso="2026-07-15T13:00:00+00:00", limit=10)
    assert any(t.id == a.id for t in due2)


@pytest.mark.asyncio
async def test_dispatch_claim_prevents_double(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=12, name="C", department="engineering")
    agent = AsyncMock()
    agent.run_headless = AsyncMock(
        return_value=HeadlessResult(
            status="completed",
            reply="ok",
            session_id=1,
            stats=RunStats(status="ok"),
            source="scheduled",
        )
    )
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        agent_service=agent,
        settings=_settings(tmp_path),
    )
    p = await svc.propose(user, title="once", task_text="hi", schedule_text="1m", notify=False)
    a = await svc.accept(user, p.id)
    a.next_run_at = "2000-01-01T00:00:00+00:00"
    await store.update(a)
    r1 = await svc._dispatch(a, now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
    assert r1["status"] == "completed"
    # Same stale next_run cannot claim again (task is done)
    a2 = await store.get(a.id, user_id=user.id)
    assert a2 is not None
    assert a2.status == "done"
    assert agent.run_headless.await_count == 1


@pytest.mark.asyncio
async def test_dedup_blocks_live_duplicate(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=13, name="D", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    await svc.propose(user, title="same", task_text="work", schedule_text="every 1h", notify=False)
    with pytest.raises(SchedulerError, match="fingerprint"):
        await svc.propose(
            user, title="same", task_text="work", schedule_text="every 1h", notify=False
        )


@pytest.mark.asyncio
async def test_ttl_expire_clears_dedup_allows_repropose(tmp_path: Path) -> None:
    """F4: TTL expiry must not permanent-latch fingerprint; re-propose works."""
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=23, name="TTL", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    first = await svc.propose(
        user, title="daily", task_text="report", schedule_text="every 1d", notify=False
    )
    # Force-expire as if pending_ttl elapsed
    n = await store.expire_pending("9999-12-31T00:00:00+00:00")
    assert n >= 1
    expired = await store.get(first.id, user_id=user.id)
    assert expired is not None
    assert expired.status == "dismissed"
    assert expired.dedup_key == ""

    second = await svc.propose(
        user, title="daily", task_text="report", schedule_text="every 1d", notify=False
    )
    assert second.status == "pending"
    assert second.id != first.id


@pytest.mark.asyncio
async def test_user_dismiss_still_latches_dedup(tmp_path: Path) -> None:
    """F4: explicit user dismiss keeps fingerprint latch."""
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=24, name="Latch", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    first = await svc.propose(
        user, title="x", task_text="body", schedule_text="every 2h", notify=False
    )
    await svc.dismiss(user, first.id)
    with pytest.raises(SchedulerError, match="dismissed earlier"):
        await svc.propose(user, title="x", task_text="body", schedule_text="every 2h", notify=False)


@pytest.mark.asyncio
async def test_task_text_cap(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=14, name="E", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    with pytest.raises(SchedulerError, match="task_text exceeds"):
        await svc.propose(
            user,
            title="t",
            task_text="x" * 200,
            schedule_text="every 1h",
            notify=False,
        )


@pytest.mark.asyncio
async def test_interval_not_immediately_due(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=15, name="F", department="engineering")
    agent = AsyncMock()
    agent.run_headless = AsyncMock(
        return_value=HeadlessResult(status="completed", reply="ok", source="scheduled")
    )
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        agent_service=agent,
        settings=_settings(tmp_path),
    )
    p = await svc.propose(user, title="iv", task_text="x", schedule_text="every 1h", notify=False)
    a = await svc.accept(user, p.id)
    assert a.next_run_at is not None
    # Not due right now
    await svc.tick()
    agent.run_headless.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_channel_denies_schedule_tool() -> None:
    """H2: execute path rejects schedule_* when channel=system."""
    import tempfile

    from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
    from corpclaw_lite.config.settings import AgentSettings, SchedulerSettings
    from corpclaw_lite.extensions.tools.builtin.schedule import ScheduleProposeTool
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.llm.base import ToolCall
    from corpclaw_lite.scheduler.service import SchedulerService
    from corpclaw_lite.scheduler.store import SchedulerStore

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        user = User(id=99, name="Z", department="engineering")
        store = SchedulerStore(tmp / "s.db")
        svc = SchedulerService(
            store=store,
            user_manager=_FakeUM(user),  # type: ignore[arg-type]
            settings=SchedulerSettings(timezone="UTC", db_path=str(tmp / "s.db")),
        )
        reg = ToolRegistry()
        reg.register(ScheduleProposeTool(svc))

        class _FakeProvider:
            async def chat(self, *a: Any, **k: Any) -> Any:
                raise AssertionError("unused")

        loop = AgentLoop(
            AgentConfig(
                provider=_FakeProvider(),  # type: ignore[arg-type]
                registry=reg,
                settings=AgentSettings(),
            )
        )
        tc = ToolCall(
            id="1",
            name="schedule_propose",
            arguments={
                "title": "t",
                "task_text": "do it",
                "schedule_text": "every 1h",
            },
        )
        result = await loop._execute_single_tool(
            tc, user, None, channel="system", emit_tool_start=False
        )
        assert "not available in headless/system" in result
        tasks = await store.list_for_user(user.id)
        assert tasks == []


# ── Status-guarded transitions (post-review hardening) ────────────────────


@pytest.mark.asyncio
async def test_update_guarded_returns_false_on_status_mismatch(tmp_path: Path) -> None:
    """update_guarded must return False when on-disk status doesn't match expected."""
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=30, name="Guard", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    p = await svc.propose(user, title="t", task_text="body", schedule_text="every 1h", notify=False)
    # Task is "pending" on disk. Try a guarded update expecting "active" → must fail.
    p.status = "active"  # simulate caller thinking it's already active
    ok = await store.update_guarded(p, expected_status="active")
    assert ok is False, "expected False — on-disk status is 'pending', not 'active'"
    # Correct expectation matches → success.
    p.status = "paused"
    ok = await store.update_guarded(p, expected_status="pending")
    assert ok is True


@pytest.mark.asyncio
async def test_accept_rejects_after_concurrent_dismiss(tmp_path: Path) -> None:
    """Cross-channel race: dismiss wins first, accept must fail (Python check or SQL guard)."""
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=31, name="Race", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    p = await svc.propose(user, title="t", task_text="body", schedule_text="every 1h", notify=False)
    # Channel A dismisses the proposal.
    await svc.dismiss(user, p.id)
    # Channel B tries to accept the same task_id. The fresh get() sees status=dismissed,
    # so the Python-level check catches it first (SQL guard is the TOCTOU safety net).
    with pytest.raises(SchedulerError):
        await svc.accept(user, p.id)


@pytest.mark.asyncio
async def test_accept_sql_guard_catches_toctou(tmp_path: Path) -> None:
    """SQL guard catches the TOCTOU window: status changed between get() and update_guarded()."""
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=33, name="TOCTOU", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    p = await svc.propose(user, title="t", task_text="body", schedule_text="every 1h", notify=False)
    # Read a snapshot while status is still "pending".
    snapshot = await store.get(p.id, user_id=user.id)
    assert snapshot is not None and snapshot.status == "pending"
    # Competitor changes status to dismissed (simulating cross-channel race).
    await svc.dismiss(user, p.id)
    # Now attempt a guarded write on the stale snapshot expecting "pending".
    snapshot.status = "active"  # simulate accept's mutation
    ok = await store.update_guarded(snapshot, expected_status="pending")
    assert ok is False, "SQL guard should reject — on-disk status is 'dismissed', not 'pending'"


@pytest.mark.asyncio
async def test_pause_resume_status_guard(tmp_path: Path) -> None:
    """pause/resume reject double-transitions (Python check or SQL guard)."""
    store = SchedulerStore(tmp_path / "s.db")
    user = User(id=32, name="PR", department="engineering")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    p = await svc.propose(user, title="t", task_text="body", schedule_text="every 1h", notify=False)
    active = await svc.accept(user, p.id)
    assert active.status == "active"

    # Pause → paused.
    paused = await svc.pause(user, p.id)
    assert paused.status == "paused"

    # Second pause: fresh get() sees "paused", Python check rejects.
    with pytest.raises(SchedulerError):
        await svc.pause(user, p.id)

    # Resume → active.
    resumed = await svc.resume(user, p.id)
    assert resumed.status == "active"

    # Second resume: fresh get() sees "active", Python check rejects.
    with pytest.raises(SchedulerError):
        await svc.resume(user, p.id)
