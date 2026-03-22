# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from corpclaw_lite.channels.base import Channel
from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)

_APPROVAL_TIMEOUT = 300.0  # seconds


class TelegramChannel(Channel):
    """Telegram communication channel for CorpClaw Lite."""

    name = "telegram"

    def __init__(self, token: str, message_handler: Callable[[str, str], Any]) -> None:
        """
        Args:
            token: Telegram Bot token
            message_handler: Callback async function(telegram_id: str, message_text: str)
        """
        self.token = token
        self._app: Application | None = None  # type: ignore
        self._on_message = message_handler
        # message_id → (Future[bool], expected_telegram_user_id)
        self._pending_approvals: dict[str, tuple[asyncio.Future[bool], int]] = {}

    async def start(self) -> None:
        """Initialize the Telegram bot application."""
        self._app = Application.builder().token(self.token).build()

        # Handlers
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        logger.info("Initializing Telegram application...")
        await self._app.initialize()
        await self._app.start()

        # Start background polling (could also be webhooks in prod)
        await self._app.updater.start_polling()  # type: ignore
        logger.info("Telegram channel started.")

    async def stop(self) -> None:
        """Stop polling and close the Telegram bot."""
        if self._app:
            await self._app.updater.stop()  # type: ignore
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram channel stopped.")

    async def send_message(self, user: User, text: str, **opts: Any) -> None:
        """Send a message to a user via Telegram."""
        if not self._app or not self._app.bot:
            return

        try:
            await self._app.bot.send_message(
                chat_id=user.telegram_id, text=text, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error("Failed to send Telegram message to %s: %s", user.telegram_id, e)

    async def send_file(self, user: User, path: Path, caption: str = "") -> None:
        """Send a document wrapper."""
        if not self._app or not self._app.bot:
            return

        try:
            with open(path, "rb") as f:
                await self._app.bot.send_document(
                    chat_id=user.telegram_id, document=f, caption=caption
                )
        except Exception as e:
            logger.error("Failed to send file to %s: %s", user.telegram_id, e)

    async def request_approval(self, user: User, action: str, details: str) -> bool:
        """
        Send a message with Approve/Deny inline buttons and wait for the user's tap.
        Returns True if approved, False if denied or timed out.
        """
        if not self._app or not self._app.bot or not user.telegram_id:
            return False

        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data="approve"),
                InlineKeyboardButton("❌ Deny", callback_data="deny"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        sent: Message = await self._app.bot.send_message(
            chat_id=user.telegram_id,
            text=f"<b>⚠️ Approval Required</b>\n\n<b>Action:</b> {action}\n\n<i>{details}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_approvals[str(sent.message_id)] = (future, user.telegram_id or 0)

        try:
            return await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT)
        except TimeoutError:
            logger.warning("Approval request timed out for action '%s'", action)
            self._pending_approvals.pop(str(sent.message_id), None)
            return False

    async def _handle_start(self, update: Update, context: Any) -> None:
        if update.effective_chat:
            await update.effective_chat.send_message("Welcome to CorpClaw Lite!")

    async def _handle_text(self, update: Update, context: Any) -> None:
        if update.message and update.message.text and update.effective_user:
            # Route back to agent
            await self._on_message(str(update.effective_user.id), update.message.text)

    async def _handle_callback(self, update: Update, context: Any) -> None:
        query = update.callback_query
        if not query or not query.message:
            return

        msg_id = str(query.message.message_id)
        entry = self._pending_approvals.get(msg_id)
        if not entry:
            await query.answer()  # silent ack for unrelated / expired callbacks
            return

        future, expected_uid = entry
        caller_uid = query.from_user.id if query.from_user else None
        if caller_uid != expected_uid:
            # First and only answer() call — show the alert
            await query.answer("This approval request is not addressed to you.", show_alert=True)
            return

        self._pending_approvals.pop(msg_id)
        approved = query.data == "approve"

        if not future.done():
            future.set_result(approved)

        await query.answer()  # silent ack after resolving the future
        label = "✅ Approved" if approved else "❌ Denied"
        await query.edit_message_text(text=label)
