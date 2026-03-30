"""Admin notifier — broadcasts messages to admin Telegram users."""

from __future__ import annotations

import logging

from telegram import Bot as TelegramBot

__all__ = [
    "AdminNotifier",
]

logger = logging.getLogger(__name__)


class AdminNotifier:
    """Broadcast important messages (errors, alerts) to admin users."""

    def __init__(self, bot: TelegramBot, admin_ids: list[int]) -> None:
        self._bot = bot
        self._admin_ids = admin_ids

    async def notify(self, message: str) -> None:
        """Send *message* to every configured admin Telegram ID."""
        if not self._admin_ids:
            return
        for admin_id in self._admin_ids:
            try:
                await self._bot.send_message(chat_id=admin_id, text=message)
            except Exception as exc:
                logger.warning("Failed to notify admin %d: %s", admin_id, exc)
