"""Tests for TelegramChannel core methods."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.channels.telegram.channel import TelegramChannel
from corpclaw_lite.users.models import User


@pytest.fixture()
def mock_bot():
    return AsyncMock()


@pytest.fixture()
def mock_app(mock_bot):
    app = MagicMock()
    app.bot = mock_bot
    return app


@pytest.fixture()
def channel(mock_app):
    async def mock_handler(tid: str, text: str, mode: str = "execute") -> None:
        pass

    ch = TelegramChannel(
        token="test_token",
        message_handler=mock_handler,
    )
    ch._app = mock_app
    return ch


class TestTelegramChannel:
    """Tests for TelegramChannel methods."""

    @pytest.mark.asyncio
    async def test_send_message_basic(self, channel: TelegramChannel, mock_bot: AsyncMock) -> None:
        user = User(id="123", telegram_id=123, department="it", name="Test User")

        await channel.send_message(user, "Hello World")

        from corpclaw_lite.channels.telegram.formatting import convert_markdown

        expected_text = convert_markdown("Hello World")

        mock_bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text=expected_text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )

    @pytest.mark.asyncio
    async def test_send_message_splits_long_text(
        self, channel: TelegramChannel, mock_bot: AsyncMock
    ) -> None:
        user = User(id="123", telegram_id=123, department="it", name="Test User")

        # Create a message long enough to be split (Telegram limit is 4096)
        # We will make it 5000 chars of 'A'
        long_message = "A" * 5000

        await channel.send_message(user, long_message)

        # Should be called multiple times (text is split into chunks)
        assert mock_bot.send_message.await_count >= 2

        # All chunks must fit Telegram's limit
        for call in mock_bot.send_message.await_args_list:
            assert len(call.kwargs["text"]) <= 4096

    @pytest.mark.asyncio
    async def test_request_approval(self, channel: TelegramChannel, mock_bot: AsyncMock) -> None:
        user = User(id="123", telegram_id=123, department="it", name="Test User")

        # Mock send_message to return a dummy message with message_id=456
        mock_msg = MagicMock()
        mock_msg.message_id = 456
        mock_bot.send_message.return_value = mock_msg

        # Create a task for request_approval
        approval_task = asyncio.create_task(
            channel.request_approval(user, "Test Action", "Details here")
        )

        # Yield to allow request_approval to set up the Future
        await asyncio.sleep(0.01)

        # The key should be "456" in pending_approvals
        key = "456"
        assert key in channel._pending_approvals

        # Resolve the future as True (Approved)
        future, expected_tid = channel._pending_approvals[key]
        assert expected_tid == 123
        future.set_result(True)

        result = await approval_task
        assert result is True

    @pytest.mark.asyncio
    async def test_request_approval_deny(
        self, channel: TelegramChannel, mock_bot: AsyncMock
    ) -> None:
        user = User(id="123", telegram_id=123, department="it", name="Test User")

        mock_msg = MagicMock()
        mock_msg.message_id = 456
        mock_bot.send_message.return_value = mock_msg

        approval_task = asyncio.create_task(channel.request_approval(user, "Delete Database", "-"))

        await asyncio.sleep(0.01)

        key = "456"
        future, _ = channel._pending_approvals[key]
        future.set_result(False)  # Denied

        result = await approval_task
        assert result is False

    @pytest.mark.asyncio
    async def test_get_user_workspace(self, channel: TelegramChannel, tmp_path: Path) -> None:
        channel._workspace_base = tmp_path
        user = User(id="999", telegram_id=999, department="it", name="Test User")

        ws = channel.get_user_workspace(user)
        assert ws.name == "user_999"
        assert ws.exists()
        assert ws.is_dir()

    @pytest.mark.asyncio
    async def test_handle_start(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.effective_chat.send_message = AsyncMock()
        await channel._handle_start(update, MagicMock())
        update.effective_chat.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_help(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.effective_chat.send_message = AsyncMock()
        channel._tool_registry = MagicMock()
        tool1 = MagicMock()
        tool1.name = "test_tool"
        tool1.description = "Test description."
        channel._tool_registry.list_all.return_value = [tool1]

        await channel._handle_help(update, MagicMock())
        update.effective_chat.send_message.assert_awaited_once()
        args = update.effective_chat.send_message.await_args[0]
        assert "test_tool" in args[0]

    @pytest.mark.asyncio
    async def test_handle_new(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.send_message = AsyncMock()
        channel._memory = AsyncMock()

        await channel._handle_new(update, MagicMock())
        channel._memory.clear.assert_awaited_once_with("123")
        update.effective_chat.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_new_marks_cache_reset(self, mock_app: MagicMock) -> None:
        async def mock_handler(tid: str, text: str, mode: str = "execute") -> None:
            pass

        cache_reset = AsyncMock()
        channel = TelegramChannel(
            token="test_token",
            message_handler=mock_handler,
            memory=AsyncMock(),
            cache_reset_callback=cache_reset,
        )
        channel._app = mock_app
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.send_message = AsyncMock()

        await channel._handle_new(update, MagicMock())

        cache_reset.assert_awaited_once_with("123")

    @pytest.mark.asyncio
    async def test_handle_chat(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.effective_chat.send_message = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        await channel._handle_chat(update, context)
        assert context.user_data["user_interaction_mode"] == "chat"
        update.effective_chat.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_execute(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.effective_chat.send_message = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        await channel._handle_execute(update, context)
        assert context.user_data["user_interaction_mode"] == "execute"
        update.effective_chat.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_text(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.effective_user.id = 123
        update.message.text = "Hello there"
        context = MagicMock()
        context.user_data = {"user_interaction_mode": "chat"}

        # Mock _is_duplicate to False
        channel._is_duplicate = AsyncMock(return_value=False)
        mock_handler = AsyncMock()
        channel._on_message = mock_handler

        await channel._handle_text(update, context)
        mock_handler.assert_awaited_once_with("123", "Hello there", "chat")

    @pytest.mark.asyncio
    async def test_send_file(
        self, channel: TelegramChannel, tmp_path: Path, mock_bot: AsyncMock
    ) -> None:
        user = User(id="123", telegram_id=123, department="it", name="Test User")
        file_path = tmp_path / "test.txt"
        file_path.write_text("file content")

        await channel.send_file(user, file_path, "Here is your file")
        mock_bot.send_document.assert_awaited_once_with(
            chat_id=123,
            document=b"file content",
            caption="Here is your file",
            filename="test.txt",
        )

    @pytest.mark.asyncio
    async def test_handle_document(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.message.document = MagicMock()
        update.effective_user.id = 123
        update.message.document.file_id = "doc123"
        update.message.document.file_name = "report.xlsx"
        update.message.document.file_size = 1024
        update.message.caption = "My doc"

        channel._is_duplicate = AsyncMock(return_value=False)
        channel._save_and_process_file = AsyncMock()

        await channel._handle_document(update, MagicMock())
        channel._save_and_process_file.assert_awaited_once_with(
            update, 123, "doc123", "report.xlsx", 1024, "My doc"
        )

    @pytest.mark.asyncio
    async def test_handle_photo(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        photo = MagicMock()
        photo.file_id = "photo123"
        photo.file_size = 2048
        update.message.photo = [MagicMock(), photo]  # Takes the last one
        update.effective_user.id = 123
        update.message.caption = "My photo"

        channel._is_duplicate = AsyncMock(return_value=False)
        channel._save_and_process_file = AsyncMock()

        await channel._handle_photo(update, MagicMock())
        channel._save_and_process_file.assert_awaited_once()
        args = channel._save_and_process_file.await_args[0]
        assert args[2] == "photo123"
        assert args[3].startswith("image_")
        assert args[3].endswith(".jpg")

    @pytest.mark.asyncio
    async def test_handle_callback_delete(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.callback_query.data = "del:ws"
        update.callback_query.answer = AsyncMock()
        context = MagicMock()
        handler = AsyncMock()
        context.user_data = {"delete_handler": handler}

        await channel._handle_callback(update, context)
        handler.handle_callback.assert_awaited_once_with(update, context, "del:ws")

    @pytest.mark.asyncio
    async def test_save_and_process_file_oversize(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        from corpclaw_lite.channels.telegram.upload import MAX_FILE_SIZE

        await channel._save_and_process_file(update, 123, "f1", "doc.pdf", MAX_FILE_SIZE + 1, None)
        update.message.reply_text.assert_awaited_once()
        assert "Файл слишком большой" in update.message.reply_text.await_args[0][0]

    @pytest.mark.asyncio
    async def test_save_and_process_file_bad_ext(self, channel: TelegramChannel) -> None:
        update = MagicMock()
        update.message.reply_text = AsyncMock()

        await channel._save_and_process_file(update, 123, "f1", "doc.exe", 1024, None)
        update.message.reply_text.assert_awaited_once()
        assert "запрещена" in update.message.reply_text.await_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_save_and_process_file_success(
        self, channel: TelegramChannel, mock_app: MagicMock, mock_bot: AsyncMock, tmp_path: Path
    ) -> None:
        update = MagicMock()
        channel._workspace_base = tmp_path
        channel._app = mock_app

        mock_file = AsyncMock()
        mock_bot.get_file.return_value = mock_file

        mock_handler = AsyncMock()
        channel._on_message = mock_handler

        await channel._save_and_process_file(update, 123, "fid", "report.pdf", 1024, "Please read")

        mock_bot.get_file.assert_awaited_once_with("fid")
        mock_file.download_to_drive.assert_awaited_once()

        mock_handler.assert_awaited_once()
        args = mock_handler.await_args[0]
        assert args[0] == "123"
        assert "report.pdf" in args[1]
        assert "Please read" in args[1]


class TestPollingRecovery:
    """Tests for polling error recovery methods."""

    def test_looks_like_polling_conflict(self) -> None:
        error = Exception("Conflict: terminated by other getUpdates request")
        assert TelegramChannel._looks_like_polling_conflict(error) is True

        class ConflictError(Exception):
            pass

        assert TelegramChannel._looks_like_polling_conflict(ConflictError("409")) is True
        assert TelegramChannel._looks_like_polling_conflict(Exception("something else")) is False

    def test_looks_like_network_error(self) -> None:
        assert TelegramChannel._looks_like_network_error(ConnectionError("reset")) is True
        assert TelegramChannel._looks_like_network_error(TimeoutError("timed out")) is True
        assert TelegramChannel._looks_like_network_error(OSError("broken pipe")) is True
        assert TelegramChannel._looks_like_network_error(ValueError("bad")) is False

    @pytest.mark.asyncio
    async def test_handle_polling_conflict_retries(self, channel: TelegramChannel) -> None:
        from corpclaw_lite.config.settings import TelegramSettings

        channel._tg_settings = TelegramSettings(conflict_max_retries=3)
        channel._polling_conflict_count = 0
        channel._polling_error_callback_ref = lambda e: None

        updater_mock = AsyncMock()
        channel._app = MagicMock()
        channel._app.updater = updater_mock

        await channel._handle_polling_conflict(Exception("Conflict: terminated"))

        updater_mock.start_polling.assert_awaited_once()
        # On success, count resets to 0
        assert channel._polling_conflict_count == 0

    @pytest.mark.asyncio
    async def test_handle_polling_conflict_raises_after_max(self, channel: TelegramChannel) -> None:
        from corpclaw_lite.config.settings import TelegramSettings

        channel._tg_settings = TelegramSettings(conflict_max_retries=3)
        channel._polling_conflict_count = 3
        channel._polling_error_callback_ref = lambda e: None

        with pytest.raises(RuntimeError, match="conflict after 3 retries"):
            await channel._handle_polling_conflict(Exception("Conflict"))

    @pytest.mark.asyncio
    async def test_handle_polling_network_error_reconnects(self, channel: TelegramChannel) -> None:
        from corpclaw_lite.config.settings import TelegramSettings

        channel._tg_settings = TelegramSettings(network_max_retries=2)
        channel._polling_network_error_count = 0
        channel._polling_error_callback_ref = lambda e: None

        updater_mock = AsyncMock()
        channel._app = MagicMock()
        channel._app.updater = updater_mock

        await channel._handle_polling_network_error(ConnectionError("reset"))

        updater_mock.start_polling.assert_awaited_once()
        # On success, count resets to 0
        assert channel._polling_network_error_count == 0

    @pytest.mark.asyncio
    async def test_handle_polling_network_error_raises_after_max(
        self, channel: TelegramChannel
    ) -> None:
        from corpclaw_lite.config.settings import TelegramSettings

        channel._tg_settings = TelegramSettings(network_max_retries=2)
        channel._polling_network_error_count = 3
        channel._polling_error_callback_ref = lambda e: None

        with pytest.raises(RuntimeError, match="unrecoverable after 2"):
            await channel._handle_polling_network_error(ConnectionError("dead"))


class TestResolveFallbackIps:
    """Tests for fallback IP resolution chain."""

    @pytest.mark.asyncio
    async def test_config_ips_take_priority(self) -> None:
        from corpclaw_lite.config.settings import TelegramSettings

        settings = TelegramSettings(fallback_ips=["1.1.1.1", "8.8.8.8"])

        async def noop(*args: object, **kwargs: object) -> None:
            pass

        ch = TelegramChannel(token="test", message_handler=noop, tg_settings=settings)
        result = await ch._resolve_fallback_ips()
        assert result == ["1.1.1.1", "8.8.8.8"]

    @pytest.mark.asyncio
    async def test_env_var_override(self) -> None:
        async def noop(*args: object, **kwargs: object) -> None:
            pass

        ch = TelegramChannel(token="test", message_handler=noop)
        with (
            patch.dict("os.environ", {"CORPCLAW_TELEGRAM_FALLBACK_IPS": "1.1.1.1"}),
            patch(
                "corpclaw_lite.channels.telegram.channel.discover_fallback_ips",
                new_callable=AsyncMock,
                return_value=["seed_ip"],
            ),
        ):
            result = await ch._resolve_fallback_ips()
            assert result == ["1.1.1.1"]

    @pytest.mark.asyncio
    async def test_build_request_kwargs_with_settings(self) -> None:
        from corpclaw_lite.config.settings import TelegramSettings

        settings = TelegramSettings(connect_timeout=5.0, read_timeout=30.0, pool_timeout=4.0)
        kwargs = TelegramChannel._build_request_kwargs(settings)
        assert kwargs == {
            "connect_timeout": 5.0,
            "read_timeout": 30.0,
            "pool_timeout": 4.0,
        }

    @pytest.mark.asyncio
    async def test_build_request_kwargs_without_settings(self) -> None:
        kwargs = TelegramChannel._build_request_kwargs(None)
        assert kwargs == {
            "connect_timeout": 10.0,
            "read_timeout": 20.0,
            "pool_timeout": 8.0,
        }
