"""B-141: schedule REST handlers on WebChannelOrchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from corpclaw_lite.channels.web.orchestrator import WebChannelOrchestrator
from corpclaw_lite.config.settings import SchedulerSettings, Settings
from corpclaw_lite.scheduler.service import SchedulerService
from corpclaw_lite.scheduler.store import SchedulerStore
from corpclaw_lite.users.models import User


class _FakeUM:
    def __init__(self, user: User) -> None:
        self._user = user

    def get_by_id(self, user_id: int) -> User | None:
        return self._user if user_id == self._user.id else None


def _request(
    method: str,
    path: str,
    user: User,
    *,
    match_info: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    query: str = "",
) -> Any:
    full = f"{path}{query}"
    request = make_mocked_request(method, full, match_info=match_info or {})
    request["user"] = user
    if payload is not None:
        request.json = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    else:
        request.json = AsyncMock(side_effect=Exception("empty"))  # type: ignore[method-assign]
    return request


@pytest.fixture
def orch_sched(tmp_path: Path) -> tuple[WebChannelOrchestrator, SchedulerService, User]:
    user = User(id=42, name="Test", department="engineering")
    settings = Settings()
    orch = WebChannelOrchestrator(settings)
    store = SchedulerStore(tmp_path / "sched.db")
    svc = SchedulerService(
        store=store,
        user_manager=_FakeUM(user),  # type: ignore[arg-type]
        agent_service=None,
        notifier=None,
        settings=SchedulerSettings(
            enabled=True,
            timezone="UTC",
            db_path=str(tmp_path / "sched.db"),
            max_tasks_per_user=3,
        ),
    )
    orch._scheduler = svc
    return orch, svc, user


@pytest.mark.asyncio
async def test_list_empty(
    orch_sched: tuple[WebChannelOrchestrator, SchedulerService, User],
) -> None:
    orch, _svc, user = orch_sched
    req = _request("GET", "/api/schedule", user)
    resp = await orch._handle_list_schedule(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["tasks"] == []


@pytest.mark.asyncio
async def test_accept_with_overrides(
    orch_sched: tuple[WebChannelOrchestrator, SchedulerService, User],
) -> None:
    orch, svc, user = orch_sched
    pending = await svc.propose(
        user,
        title="old",
        task_text="do old",
        schedule_text="every 1h",
        notify=False,
    )
    req = _request(
        "POST",
        f"/api/schedule/{pending.id}/accept",
        user,
        match_info={"id": pending.id},
        payload={
            "title": "new title",
            "task_text": "do new",
            "schedule_text": "every 2h",
        },
    )
    resp = await orch._handle_schedule_accept(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    task = body["task"]
    assert task["status"] == "active"
    assert task["title"] == "new title"
    assert task["task_text"] == "do new"
    assert task["schedule_text"] == "every 2h"
    assert task["next_run_at"] is not None
    assert "claim_token" not in task


@pytest.mark.asyncio
async def test_dismiss_and_pause_resume(
    orch_sched: tuple[WebChannelOrchestrator, SchedulerService, User],
) -> None:
    orch, svc, user = orch_sched
    p = await svc.propose(user, title="t", task_text="x", schedule_text="every 1h", notify=False)
    # dismiss pending
    req = _request(
        "POST",
        f"/api/schedule/{p.id}/dismiss",
        user,
        match_info={"id": p.id},
    )
    resp = await orch._handle_schedule_dismiss(req)
    assert resp.status == 200
    assert json.loads(resp.text)["task"]["status"] == "dismissed"

    p2 = await svc.propose(user, title="t2", task_text="y", schedule_text="every 1h", notify=False)
    a = await svc.accept(user, p2.id)
    pause_req = _request(
        "POST",
        f"/api/schedule/{a.id}/pause",
        user,
        match_info={"id": a.id},
    )
    pause_resp = await orch._handle_schedule_pause(pause_req)
    assert json.loads(pause_resp.text)["task"]["status"] == "paused"

    resume_req = _request(
        "POST",
        f"/api/schedule/{a.id}/resume",
        user,
        match_info={"id": a.id},
    )
    resume_resp = await orch._handle_schedule_resume(resume_req)
    assert json.loads(resume_resp.text)["task"]["status"] == "active"


@pytest.mark.asyncio
async def test_get_404_wrong_user(
    orch_sched: tuple[WebChannelOrchestrator, SchedulerService, User],
) -> None:
    orch, svc, user = orch_sched
    other = User(id=99, name="Other", department="engineering")
    p = await svc.propose(user, title="t", task_text="x", schedule_text="every 1h", notify=False)
    req = _request(
        "GET",
        f"/api/schedule/{p.id}",
        other,
        match_info={"id": p.id},
    )
    with pytest.raises(Exception) as exc_info:
        await orch._handle_get_schedule(req)
    # aiohttp HTTPNotFound
    assert (
        getattr(exc_info.value, "status", None) == 404 or "not found" in str(exc_info.value).lower()
    )


@pytest.mark.asyncio
async def test_accept_bad_schedule_400(
    orch_sched: tuple[WebChannelOrchestrator, SchedulerService, User],
) -> None:
    orch, svc, user = orch_sched
    p = await svc.propose(
        user,
        title="t",
        task_text="x",
        schedule_text="not a real schedule xyz",
        notify=False,
    )
    req = _request(
        "POST",
        f"/api/schedule/{p.id}/accept",
        user,
        match_info={"id": p.id},
        payload={},
    )
    resp = await orch._handle_schedule_accept(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert "error" in body


@pytest.mark.asyncio
async def test_list_filter_status(
    orch_sched: tuple[WebChannelOrchestrator, SchedulerService, User],
) -> None:
    orch, svc, user = orch_sched
    await svc.propose(user, title="p1", task_text="a", schedule_text="every 1h", notify=False)
    p2 = await svc.propose(user, title="p2", task_text="b", schedule_text="every 1h", notify=False)
    await svc.accept(user, p2.id)
    req = _request("GET", "/api/schedule?status=pending", user)
    resp = await orch._handle_list_schedule(req)
    body = json.loads(resp.text)
    assert all(t["status"] == "pending" for t in body["tasks"])
    assert len(body["tasks"]) == 1


@pytest.mark.asyncio
async def test_503_without_scheduler(
    orch_sched: tuple[WebChannelOrchestrator, SchedulerService, User],
) -> None:
    orch, _svc, user = orch_sched
    orch._scheduler = None
    req = _request("GET", "/api/schedule", user)
    with pytest.raises(Exception) as exc_info:
        await orch._handle_list_schedule(req)
    assert getattr(exc_info.value, "status", None) == 503
