import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.channels.cli import CLIChannel
from corpclaw_lite.channels.telegram_channel import TelegramChannel
from corpclaw_lite.users.models import User


@pytest.fixture
def test_user():
    return User(
        id=1,
        name="Test User",
        telegram_id=123456789,
        department="IT",
    )


@pytest.mark.asyncio
async def test_cli_channel(capsys: pytest.CaptureFixture[str], test_user: User) -> None:
    channel = CLIChannel()
    await channel.start()
    out, err = capsys.readouterr()
    assert "CLI Channel started" in out

    await channel.send_message(test_user, "Hello World")
    out, err = capsys.readouterr()
    assert "Hello World" in out
    assert "Test User" in out

    await channel.send_file(test_user, Path("test.txt"), "caption")
    out, err = capsys.readouterr()
    assert "Sending file to Test User" in out
    assert "test.txt" in out
    assert "caption" in out

    await channel.stop()
    out, err = capsys.readouterr()
    assert "CLI Channel stopped" in out


@pytest.mark.asyncio
async def test_telegram_approval_approved(test_user: User) -> None:
    """Approval future resolves True when callback fires with 'approve'."""
    channel = TelegramChannel(token="test-token", message_handler=AsyncMock())

    # Mock bot.send_message to return a message with a known message_id
    sent_msg = MagicMock()
    sent_msg.message_id = 42

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=sent_msg)

    mock_app = MagicMock()
    mock_app.bot = mock_bot
    channel._app = mock_app

    # Schedule the callback resolution slightly after request_approval starts
    async def _fire_callback() -> None:
        await asyncio.sleep(0.05)
        entry = channel._pending_approvals.get("42")
        if entry:
            future, _ = entry
            if not future.done():
                future.set_result(True)

    asyncio.create_task(_fire_callback())
    result = await channel.request_approval(test_user, "delete_file", "Delete /tmp/test.txt")
    assert result is True


@pytest.mark.asyncio
async def test_telegram_approval_denied(test_user: User) -> None:
    """Approval future resolves False when callback fires with 'deny'."""
    channel = TelegramChannel(token="test-token", message_handler=AsyncMock())

    sent_msg = MagicMock()
    sent_msg.message_id = 99

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=sent_msg)

    mock_app = MagicMock()
    mock_app.bot = mock_bot
    channel._app = mock_app

    async def _fire_callback() -> None:
        await asyncio.sleep(0.05)
        entry = channel._pending_approvals.get("99")
        if entry:
            future, _ = entry
            if not future.done():
                future.set_result(False)

    asyncio.create_task(_fire_callback())
    result = await channel.request_approval(test_user, "exec_script", "Run install.sh")
    assert result is False


@pytest.mark.asyncio
async def test_approval_wrong_user_rejected(test_user: User) -> None:
    """Callback from a different Telegram user_id must NOT resolve the future."""
    channel = TelegramChannel(token="test-token", message_handler=AsyncMock())

    sent_msg = MagicMock()
    sent_msg.message_id = 77

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=sent_msg)

    mock_app = MagicMock()
    mock_app.bot = mock_bot
    channel._app = mock_app

    # Fire callback from a DIFFERENT user (uid=999999, expected=123456789)
    async def _fire_wrong_user_callback() -> None:
        await asyncio.sleep(0.05)
        from unittest.mock import MagicMock as MM

        from telegram import Update

        query = MM()
        query.message = MM()
        query.message.message_id = 77
        query.from_user = MM()
        query.from_user.id = 999999  # wrong user
        query.data = "approve"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MM(spec=Update)
        update.callback_query = query
        await channel._handle_callback(update, None)

    # Fire a correct callback after 0.15s to actually resolve
    async def _fire_correct_callback() -> None:
        await asyncio.sleep(0.15)
        entry = channel._pending_approvals.get("77")
        if entry:
            future, _ = entry
            if not future.done():
                future.set_result(False)

    asyncio.create_task(_fire_wrong_user_callback())
    asyncio.create_task(_fire_correct_callback())

    result = await channel.request_approval(test_user, "risky_action", "details")
    # Future was NOT resolved by wrong-user callback (at 0.05s),
    # but was resolved by correct callback (at 0.15s) with False
    assert result is False


@pytest.mark.asyncio
async def test_handle_callback_wrong_user_shows_alert(test_user: User) -> None:
    """Wrong user callback: answer() must be called exactly once with show_alert=True."""
    from unittest.mock import MagicMock as MM

    from telegram import Update

    channel = TelegramChannel(token="test-token", message_handler=AsyncMock())

    # Pre-populate a pending approval for message_id=55, expected user=123456789
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    channel._pending_approvals["55"] = (future, 123456789)

    answer_mock = AsyncMock()

    query = MM()
    query.message = MM()
    query.message.message_id = 55
    query.from_user = MM()
    query.from_user.id = 999999  # wrong user
    query.data = "approve"
    query.answer = answer_mock
    query.edit_message_text = AsyncMock()

    update = MM(spec=Update)
    update.callback_query = query

    await channel._handle_callback(update, None)

    # answer() called exactly once, with show_alert=True
    answer_mock.assert_called_once()
    _, kwargs = answer_mock.call_args
    assert kwargs.get("show_alert") is True, "show_alert must be True for wrong-user rejection"

    # Future must NOT be resolved
    assert not future.done(), "Future must remain unresolved after wrong-user callback"
