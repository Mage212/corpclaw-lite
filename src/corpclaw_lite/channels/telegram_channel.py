# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from corpclaw_lite.channels.base import Channel
from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)


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
            logger.error(f"Failed to send Telegram message to {user.telegram_id}: {e}")

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
            logger.error(f"Failed to send file to {user.telegram_id}: {e}")

    async def request_approval(self, user: User, action: str, details: str) -> bool:
        """
        Send a message with inline buttons for approval.
        Wait for callback response.
        """
        # Note: Implementing a true async block for an inline button response
        # requires tracking futures per message_id.
        # For simplicity in this mock protocol, we return False for now until
        # full Future routing is implemented.
        logger.warning("Inline approval requested but blocking await not fully implemented.")
        keyboard = [
            [
                InlineKeyboardButton("Approve", callback_data=f"approve_{action}"),
                InlineKeyboardButton("Deny", callback_data=f"deny_{action}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if self._app and self._app.bot:
            await self._app.bot.send_message(
                chat_id=user.telegram_id,
                text=f"<b>Approval Required:</b> {action}\n\n<i>{details}</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )

        # Returning false immediately to prevent hangs in Phase 2 skeleton.
        # This will be replaced with a proper asyncio.Future wait.
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
        if query:
            await query.answer()
            # We would resolve the waiting Future here
            await query.edit_message_text(text=f"Action chosen: {query.data}")
