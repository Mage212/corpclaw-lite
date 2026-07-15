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
