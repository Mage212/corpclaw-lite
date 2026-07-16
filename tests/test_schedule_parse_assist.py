"""B-143 PR3: schedule parse-assist (deterministic + LLM path)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from corpclaw_lite.channels.web.orchestrator import WebChannelOrchestrator
from corpclaw_lite.config.settings import SchedulerSettings, Settings
from corpclaw_lite.llm.base import LLMResponse, TokenUsage
from corpclaw_lite.scheduler.parse_assist import ParseAssistError, llm_parse_schedule
from corpclaw_lite.scheduler.service import SchedulerError, SchedulerService
from corpclaw_lite.scheduler.store import SchedulerStore
from corpclaw_lite.users.models import User


class _FakeUM:
    def __init__(self, user: User) -> None:
        self._user = user

    def get_by_id(self, user_id: int) -> User | None:
        return self._user if user_id == self._user.id else None


def _settings(tmp_path: Path) -> SchedulerSettings:
    return SchedulerSettings(
        enabled=True,
        timezone="UTC",
        db_path=str(tmp_path / "sched.db"),
        max_tasks_per_user=5,
    )


class _FakeProvider:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=self.content,
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


@pytest.mark.asyncio
async def test_parse_assist_skips_llm_when_deterministic(tmp_path: Path) -> None:
    user = User(id=1, name="A", department="engineering")
    store = SchedulerStore(tmp_path / "s.db")
    provider = _FakeProvider('{"formula":"every 1h","explanation":"x"}')
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
        provider=provider,  # type: ignore[arg-type]
    )
    task = await svc.propose(
        user,
        title="t",
        task_text="do",
        schedule_text="every 2h",
        notify=False,
    )
    result = await svc.parse_assist(user, task.id)
    assert result.formula == "every 2h"
    assert result.schedule.kind == "interval"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_llm_parse_schedule_validates_formula() -> None:
    provider = _FakeProvider('{"formula": "every 30m", "explanation": "каждые полчаса"}')
    result = await llm_parse_schedule(
        provider,  # type: ignore[arg-type]
        schedule_text="каждые полчаса",
        timezone="UTC",
    )
    assert result.formula == "every 30m"
    assert result.schedule.kind == "interval"
    assert result.schedule.minutes == 30
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_llm_parse_rejects_bad_formula() -> None:
    provider = _FakeProvider('{"formula": "not a real schedule", "explanation": "oops"}')
    with pytest.raises(ParseAssistError, match="not parseable"):
        await llm_parse_schedule(
            provider,  # type: ignore[arg-type]
            schedule_text="sometime",
            timezone="UTC",
        )


@pytest.mark.asyncio
async def test_parse_assist_service_uses_llm_for_unset(tmp_path: Path) -> None:
    user = User(id=2, name="B", department="engineering")
    store = SchedulerStore(tmp_path / "s.db")
    provider = _FakeProvider('{"formula":"every 1d","explanation":"раз в сутки"}')
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
        provider=provider,  # type: ignore[arg-type]
    )
    task = await svc.propose(
        user,
        title="t",
        task_text="do",
        schedule_text="каждый день утром примерно",
        notify=False,
    )
    assert task.schedule.kind == "unset"
    result = await svc.parse_assist(user, task.id)
    assert result.formula == "every 1d"
    assert provider.calls == 1
    # Still pending until accept with formula
    still = await store.get(task.id, user_id=user.id)
    assert still is not None
    assert still.status == "pending"


@pytest.mark.asyncio
async def test_parse_assist_rest_handler(tmp_path: Path) -> None:
    user = User(id=3, name="C", department="engineering")
    settings = Settings()
    orch = WebChannelOrchestrator(settings)
    store = SchedulerStore(tmp_path / "s.db")
    provider = _FakeProvider('{"formula":"every 1h","explanation":"час"}')
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
        provider=provider,  # type: ignore[arg-type]
    )
    orch._scheduler = svc
    task = await svc.propose(
        user,
        title="t",
        task_text="x",
        schedule_text="примерно каждый час",
        notify=False,
    )
    req = make_mocked_request(
        "POST",
        f"/api/schedule/{task.id}/parse-assist",
        match_info={"id": task.id},
    )
    req["user"] = user
    req.json = AsyncMock(return_value={})  # type: ignore[method-assign]
    resp = await orch._handle_schedule_parse_assist(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["assist"]["formula"] == "every 1h"
    assert body["task_id"] == task.id


@pytest.mark.asyncio
async def test_parse_assist_requires_pending(tmp_path: Path) -> None:
    user = User(id=4, name="D", department="engineering")
    store = SchedulerStore(tmp_path / "s.db")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )
    task = await svc.propose(user, title="t", task_text="x", schedule_text="every 1h", notify=False)
    await svc.accept(user, task.id)
    with pytest.raises(SchedulerError, match="not pending"):
        await svc.parse_assist(user, task.id)
