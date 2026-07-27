"""B-121 / DC-025a: FeedbackStore — record, UPSERT, allow_change, get, list_for_run."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.feedback.store import FeedbackStore


@pytest.fixture()
def store(tmp_path: Path) -> FeedbackStore:
    return FeedbackStore(tmp_path / "feedback.db")


@pytest.mark.asyncio
async def test_record_inserts_first_vote(store: FeedbackStore) -> None:
    label = await store.record(
        run_id="run-1",
        user_id="user-1",
        rating="up",
        channel="web",
        message_ref=None,
    )
    assert label.run_id == "run-1"
    assert label.user_id == "user-1"
    assert label.rating == "up"
    assert label.channel == "web"
    assert label.message_ref is None
    # created_at and updated_at equal on first insert.
    assert label.created_at == label.updated_at


@pytest.mark.asyncio
async def test_record_upsert_allows_change(store: FeedbackStore) -> None:
    """allow_change=True (default) → re-vote overwrites the previous rating."""
    first = await store.record(
        run_id="run-1",
        user_id="user-1",
        rating="up",
        channel="web",
        message_ref=None,
    )
    second = await store.record(
        run_id="run-1",
        user_id="user-1",
        rating="down",
        channel="web",
        message_ref=None,
        allow_change=True,
    )
    assert second.rating == "down"
    assert second.created_at == first.created_at  # created_at preserved
    assert second.updated_at >= first.updated_at  # updated_at bumped


@pytest.mark.asyncio
async def test_record_disallows_change_when_flag_off(store: FeedbackStore) -> None:
    """allow_change=False → re-vote is a no-op, returns the existing label."""
    await store.record(
        run_id="run-1",
        user_id="user-1",
        rating="up",
        channel="web",
        message_ref=None,
    )
    result = await store.record(
        run_id="run-1",
        user_id="user-1",
        rating="down",
        channel="web",
        message_ref=None,
        allow_change=False,
    )
    # The original rating is preserved.
    assert result.rating == "up"


@pytest.mark.asyncio
async def test_record_is_per_user(store: FeedbackStore) -> None:
    """Same run_id, different users → two independent labels."""
    await store.record(
        run_id="run-1", user_id="user-1", rating="up", channel="web", message_ref=None
    )
    await store.record(
        run_id="run-1", user_id="user-2", rating="down", channel="web", message_ref=None
    )
    labels = await store.list_for_run("run-1")
    assert len(labels) == 2
    ratings = {lbl.user_id: lbl.rating for lbl in labels}
    assert ratings == {"user-1": "up", "user-2": "down"}


@pytest.mark.asyncio
async def test_get_returns_none_for_unrated(store: FeedbackStore) -> None:
    assert await store.get("run-1", "user-1") is None


@pytest.mark.asyncio
async def test_get_returns_existing_label(store: FeedbackStore) -> None:
    await store.record(
        run_id="run-1",
        user_id="user-1",
        rating="down",
        channel="telegram",
        message_ref="42",
    )
    fetched = await store.get("run-1", "user-1")
    assert fetched is not None
    assert fetched.rating == "down"
    assert fetched.channel == "telegram"
    assert fetched.message_ref == "42"


@pytest.mark.asyncio
async def test_list_for_run_empty(store: FeedbackStore) -> None:
    assert await store.list_for_run("nope") == []


@pytest.mark.asyncio
async def test_record_rejects_invalid_rating(store: FeedbackStore) -> None:
    with pytest.raises(ValueError, match="Invalid rating"):
        await store.record(
            run_id="run-1",
            user_id="user-1",
            rating="sideways",  # type: ignore[arg-type]
            channel="web",
            message_ref=None,
        )


@pytest.mark.asyncio
async def test_record_rejects_invalid_channel(store: FeedbackStore) -> None:
    with pytest.raises(ValueError, match="Invalid channel"):
        await store.record(
            run_id="run-1",
            user_id="user-1",
            rating="up",
            channel="carrier-pigeon",  # type: ignore[arg-type]
            message_ref=None,
        )


@pytest.mark.asyncio
async def test_persists_message_ref_telegram(store: FeedbackStore) -> None:
    """Telegram stores the message_id; web stores None. Both round-trip."""
    await store.record(
        run_id="run-tg",
        user_id="user-1",
        rating="up",
        channel="telegram",
        message_ref="12345",
    )
    fetched = await store.get("run-tg", "user-1")
    assert fetched is not None
    assert fetched.message_ref == "12345"
    assert fetched.channel == "telegram"


@pytest.mark.asyncio
async def test_schema_creation_is_idempotent(tmp_path: Path) -> None:
    """Constructing the store twice on the same path must not raise."""
    db = tmp_path / "feedback.db"
    FeedbackStore(db)
    # Second construction re-runs CREATE TABLE IF NOT EXISTS — should be a no-op.
    second = FeedbackStore(db)
    # And the store remains usable.
    await second.record(
        run_id="run-1", user_id="user-1", rating="up", channel="web", message_ref=None
    )
