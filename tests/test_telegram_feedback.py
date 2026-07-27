"""B-121: Telegram 👍/👎 callback_data parsing + channel wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.channels.telegram.callback_data import (
    CB_FB_DOWN,
    CB_FB_UP,
    feedback_down_data,
    feedback_up_data,
    parse_feedback_callback,
)
from corpclaw_lite.channels.telegram.channel import TelegramChannel
from corpclaw_lite.users.models import User

# ── callback_data unit tests ─────────────────────────────────────────────


class TestFeedbackCallbackData:
    def test_up_data_format(self) -> None:
        assert feedback_up_data("abc123") == f"{CB_FB_UP}abc123"

    def test_down_data_format(self) -> None:
        assert feedback_down_data("abc123") == f"{CB_FB_DOWN}abc123"

    @pytest.mark.parametrize(
        "data,expected",
        [
            ("fb:up:abc123", ("up", "abc123")),
            ("fb:down:deadbeef", ("down", "deadbeef")),
            ("fb:up:", None),  # empty run_id → None
            ("fb:down:", None),
            ("sc:a:xyz", None),  # schedule prefix not feedback
            ("approve", None),
            ("", None),
        ],
    )
    def test_parse(self, data: str, expected: tuple[str, str] | None) -> None:
        assert parse_feedback_callback(data) == expected

    def test_run_id_fits_telegram_limit(self) -> None:
        """Telegram callback_data limit is 64 bytes. run_id is uuid4().hex = 32 chars."""
        run_id = "a" * 32  # uuid4().hex length
        data = feedback_up_data(run_id)
        assert len(data.encode("utf-8")) <= 64
        data_down = feedback_down_data(run_id)
        assert len(data_down.encode("utf-8")) <= 64


# ── channel wiring tests ─────────────────────────────────────────────────


@pytest.fixture()
def mock_bot() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_app(mock_bot: AsyncMock) -> MagicMock:
    app = MagicMock()
    app.bot = mock_bot
    return app


@pytest.fixture()
def channel(mock_app: MagicMock) -> TelegramChannel:
    async def mock_handler(tid: str, text: str, mode: str = "execute") -> None:
        pass

    ch = TelegramChannel(token="test_token", message_handler=mock_handler)
    ch._app = mock_app
    return ch


@pytest.mark.asyncio
async def test_handle_feedback_callback_calls_handler(channel: TelegramChannel) -> None:
    """A 👍 tap dispatches to the registered feedback handler."""
    handler = AsyncMock()
    channel.set_feedback_handler(handler)

    update = MagicMock()
    update.callback_query.data = feedback_up_data("run-abc")
    update.callback_query.from_user.id = 4242
    update.callback_query.message.message_id = 999
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    context = MagicMock()

    await channel._handle_callback(update, context)

    handler.assert_awaited_once_with(4242, "up", "run-abc", 999)
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_handle_feedback_callback_down(channel: TelegramChannel, mock_bot: AsyncMock) -> None:
    """A 👎 tap records rating=down."""
    handler = AsyncMock()
    channel.set_feedback_handler(handler)

    update = MagicMock()
    update.callback_query.data = feedback_down_data("run-xyz")
    update.callback_query.from_user.id = 4242
    update.callback_query.message.message_id = 1000
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    context = MagicMock()

    await channel._handle_callback(update, context)

    handler.assert_awaited_once_with(4242, "down", "run-xyz", 1000)


@pytest.mark.asyncio
async def test_handle_feedback_callback_silent_when_no_handler(
    channel: TelegramChannel,
) -> None:
    """No handler registered → ack the tap silently, don't crash."""
    update = MagicMock()
    update.callback_query.data = feedback_up_data("run-abc")
    update.callback_query.from_user.id = 4242
    update.callback_query.message.message_id = 999
    update.callback_query.answer = AsyncMock()
    context = MagicMock()

    # Should not raise.
    await channel._handle_callback(update, context)
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_message_without_reply_markup_is_unchanged(
    channel: TelegramChannel, mock_bot: AsyncMock
) -> None:
    """Backward compat: no reply_markup kwarg → send as before (no reply_markup)."""
    user = User(id="123", telegram_id=123, department="it", name="Test User")
    await channel.send_message(user, "Hello")

    call = mock_bot.send_message.await_args
    assert call is not None
    assert "reply_markup" not in call.kwargs


@pytest.mark.asyncio
async def test_send_message_attaches_reply_markup_to_last_part(
    channel: TelegramChannel, mock_bot: AsyncMock
) -> None:
    """Multi-part message → reply_markup on the LAST part only."""
    user = User(id="123", telegram_id=123, department="it", name="Test User")
    markup = MagicMock(name="InlineKeyboardMarkup")
    long_text = "A" * 5000  # forces split

    result = await channel.send_message(user, long_text, reply_markup=markup)

    calls = mock_bot.send_message.await_args_list
    assert len(calls) >= 2
    # All parts except the last must NOT carry reply_markup.
    for call in calls[:-1]:
        assert "reply_markup" not in call.kwargs
    # The last part must carry it.
    assert calls[-1].kwargs["reply_markup"] is markup
    # And the return value is the Message of the last send.
    assert result is mock_bot.send_message.return_value


@pytest.mark.asyncio
async def test_send_message_returns_none_when_no_bot() -> None:
    """If the bot is not wired up, send returns None instead of raising."""
    channel = TelegramChannel(token="t", message_handler=lambda *a, **k: None)  # type: ignore[assignment]
    channel._app = None  # not started
    user = User(id="1", telegram_id=1, department="it", name="x")
    result = await channel.send_message(user, "hi")
    assert result is None
