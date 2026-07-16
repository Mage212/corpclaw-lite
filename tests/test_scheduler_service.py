"""B-118: SchedulerStore + SchedulerService consent and tick."""

from __future__ import annotations

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
    )


@pytest.mark.asyncio
async def test_propose_accept_limit(tmp_path: Path) -> None:
    user = User(id=1, name="A", department="engineering")
    store = SchedulerStore(tmp_path / "s.db")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        agent_service=None,
        notifier=None,
        settings=_settings(tmp_path),
    )
    for i in range(3):
        await svc.propose(
            user,
            title=f"t{i}",
            task_text=f"do {i}",
            schedule_text="every 1h",
            notify=False,
        )
    with pytest.raises(SchedulerError, match="Limit"):
        await svc.propose(
            user,
            title="t3",
            task_text="do 3",
            schedule_text="every 1h",
            notify=False,
        )

    tasks = await svc.list_for_user(user, statuses=["pending"])
    assert len(tasks) == 3
    accepted = await svc.accept(user, tasks[0].id)
    assert accepted.status == "active"
    assert accepted.next_run_at is not None


@pytest.mark.asyncio
async def test_propose_notify_schedule_confirm_metadata(tmp_path: Path) -> None:
    """B-143: propose notify includes schedule_confirm metadata for web card."""
    user = User(id=7, name="N", department="engineering")
    store = SchedulerStore(tmp_path / "s.db")
    notifier = AsyncMock()
    notifier.notify = AsyncMock(return_value=None)
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
        notifier=notifier,
    )
    task = await svc.propose(
        user,
        title="Ежедневный отчёт",
        task_text="собери метрики",
        schedule_text="every 1d",
        notify=True,
    )
    notifier.notify.assert_awaited_once()
    kwargs = notifier.notify.await_args.kwargs
    assert kwargs.get("source") == "schedule_propose"
    extra = kwargs.get("extra_metadata")
    assert isinstance(extra, dict)
    assert extra.get("kind") == "schedule_confirm"
    assert extra.get("task_id") == task.id
    assert extra.get("title") == "Ежедневный отчёт"
    assert extra.get("schedule_text") == "every 1d"
    assert extra.get("status") == "pending"


@pytest.mark.asyncio
async def test_dismiss_dedup_blocks_repropose(tmp_path: Path) -> None:
    user = User(id=2, name="B", department="engineering")
    store = SchedulerStore(tmp_path / "s.db")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    t = await svc.propose(
        user,
        title="same",
        task_text="work",
        schedule_text="every 1h",
        notify=False,
    )
    await svc.dismiss(user, t.id)
    with pytest.raises(SchedulerError, match="dismissed"):
        await svc.propose(
            user,
            title="same",
            task_text="work",
            schedule_text="every 1h",
            notify=False,
        )


@pytest.mark.asyncio
async def test_tick_headless_once_done(tmp_path: Path) -> None:
    user = User(id=3, name="C", department="engineering")
    store = SchedulerStore(tmp_path / "s.db")
    agent = AsyncMock()
    agent.run_headless = AsyncMock(
        return_value=HeadlessResult(
            status="completed",
            reply="done",
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
    pending = await svc.propose(
        user,
        title="once",
        task_text="Say hi",
        schedule_text="1m",
        notify=False,
    )
    # Force due now
    active = await svc.accept(user, pending.id)
    active.next_run_at = "2000-01-01T00:00:00+00:00"
    await store.update(active)

    await svc.tick()
    agent.run_headless.assert_awaited()
    call_kw = agent.run_headless.await_args.kwargs
    assert "Контекст запуска по расписанию" in call_kw["task"]
    assert call_kw["source"] == "scheduled"

    done = await store.get(active.id, user_id=user.id)
    assert done is not None
    assert done.status == "done"


@pytest.mark.asyncio
async def test_tick_busy_defers(tmp_path: Path) -> None:
    user = User(id=4, name="D", department="engineering")
    store = SchedulerStore(tmp_path / "s.db")
    agent = AsyncMock()
    agent.run_headless = AsyncMock(
        return_value=HeadlessResult(
            status="skipped",
            skip_reason="user_busy",
            source="scheduled",
        )
    )
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        agent_service=agent,
        settings=_settings(tmp_path),
    )
    p = await svc.propose(
        user,
        title="busy",
        task_text="x",
        schedule_text="every 1h",
        notify=False,
    )
    a = await svc.accept(user, p.id)
    a.next_run_at = "2000-01-01T00:00:00+00:00"
    await store.update(a)
    await svc.tick()
    again = await store.get(a.id, user_id=user.id)
    assert again is not None
    assert again.status == "active"
    assert again.last_status == "skipped_busy"
    assert again.next_run_at is not None
    assert again.next_run_at > "2000-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_accept_override_task_text(tmp_path: Path) -> None:
    user = User(id=5, name="E", department="engineering")
    store = SchedulerStore(tmp_path / "s.db")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    p = await svc.propose(
        user,
        title="t",
        task_text="old",
        schedule_text="every 30m",
        notify=False,
    )
    a = await svc.accept(user, p.id, task_text="new text")
    assert a.task_text == "new text"
    assert a.status == "active"
