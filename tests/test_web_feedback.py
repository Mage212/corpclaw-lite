"""B-121: WebChannelOrchestrator POST /api/feedback handler + run_id in metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from corpclaw_lite.channels.web.orchestrator import WebChannelOrchestrator
from corpclaw_lite.config.settings import Settings
from corpclaw_lite.feedback.store import FeedbackStore
from corpclaw_lite.users.models import User


def _request(
    method: str,
    path: str,
    user: User,
    *,
    payload: dict[str, Any] | None = None,
) -> Any:
    request = make_mocked_request(method, path)
    request["user"] = user
    if payload is not None:
        request.json = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    else:
        request.json = AsyncMock(side_effect=Exception("empty"))  # type: ignore[method-assign]
    return request


@pytest.fixture
def orch_fb(tmp_path: Path) -> tuple[WebChannelOrchestrator, FeedbackStore, User]:
    user = User(id=42, name="Test", department="engineering")
    settings = Settings()
    orch = WebChannelOrchestrator(settings)
    store = FeedbackStore(tmp_path / "feedback.db")
    orch._feedback_store = store
    return orch, store, user


@pytest.mark.asyncio
async def test_feedback_up_returns_200(
    orch_fb: tuple[WebChannelOrchestrator, FeedbackStore, User],
) -> None:
    orch, store, user = orch_fb
    req = _request(
        "POST",
        "/api/feedback",
        user,
        payload={"run_id": "run-1", "rating": "up"},
    )
    resp = await orch._handle_feedback(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body == {"ok": True, "rating": "up"}
    # And it persisted.
    fetched = await store.get("run-1", str(user.id))
    assert fetched is not None
    assert fetched.rating == "up"
    assert fetched.channel == "web"
    assert fetched.message_ref is None


@pytest.mark.asyncio
async def test_feedback_down_persists(
    orch_fb: tuple[WebChannelOrchestrator, FeedbackStore, User],
) -> None:
    orch, store, user = orch_fb
    req = _request(
        "POST",
        "/api/feedback",
        user,
        payload={"run_id": "run-1", "rating": "down"},
    )
    resp = await orch._handle_feedback(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["rating"] == "down"
    fetched = await store.get("run-1", str(user.id))
    assert fetched is not None
    assert fetched.rating == "down"


@pytest.mark.asyncio
async def test_feedback_upsert_reflects_change(
    orch_fb: tuple[WebChannelOrchestrator, FeedbackStore, User],
) -> None:
    """allow_change=True (default) → re-vote overwrites and the response shows it."""
    orch, _store, user = orch_fb
    req_up = _request(
        "POST", "/api/feedback", user, payload={"run_id": "run-1", "rating": "up"}
    )
    await orch._handle_feedback(req_up)
    req_down = _request(
        "POST", "/api/feedback", user, payload={"run_id": "run-1", "rating": "down"}
    )
    resp = await orch._handle_feedback(req_down)
    body = json.loads(resp.text)
    assert body["rating"] == "down"


@pytest.mark.asyncio
async def test_feedback_rejects_missing_run_id(
    orch_fb: tuple[WebChannelOrchestrator, FeedbackStore, User],
) -> None:
    orch, _store, user = orch_fb
    req = _request("POST", "/api/feedback", user, payload={"rating": "up"})
    resp = await orch._handle_feedback(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert "run_id" in body["error"]


@pytest.mark.asyncio
async def test_feedback_rejects_invalid_rating(
    orch_fb: tuple[WebChannelOrchestrator, FeedbackStore, User],
) -> None:
    orch, _store, user = orch_fb
    req = _request(
        "POST",
        "/api/feedback",
        user,
        payload={"run_id": "run-1", "rating": "sideways"},
    )
    resp = await orch._handle_feedback(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_feedback_rejects_invalid_json(
    orch_fb: tuple[WebChannelOrchestrator, FeedbackStore, User],
) -> None:
    orch, _store, user = orch_fb
    req = _request("POST", "/api/feedback", user)  # no payload → json() raises
    resp = await orch._handle_feedback(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_feedback_503_when_store_not_wired(
    orch_fb: tuple[WebChannelOrchestrator, FeedbackStore, User],
) -> None:
    """When feedback is disabled (store=None), the endpoint returns 503."""
    orch, _store, user = orch_fb
    orch._feedback_store = None
    req = _request(
        "POST",
        "/api/feedback",
        user,
        payload={"run_id": "run-1", "rating": "up"},
    )
    with pytest.raises(Exception) as exc_info:
        await orch._handle_feedback(req)
    # _require_feedback_store raises HTTPServiceUnavailable.
    assert "unavailable" in str(exc_info.value).lower()
