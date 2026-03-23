"""Telegram progress indicator — temporary status messages during agent execution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_TOOL_STATUS_MAP: dict[str, str] = {
    "list_files": "📂 Читаю файл...",
    "read_file": "📂 Читаю файл...",
    "read_image": "🖼️ Просматриваю изображение...",
    "normalize_excel": "📊 Обрабатываю таблицу...",
    "web_fetch": "🌐 Ищу информацию...",
    "exec_script": "💻 Запускаю команду...",
    "exec_command": "💻 Запускаю команду...",
    "send_file_to_user": "📎 Готовлю файл...",
    "write_file": "✏️ Записываю файл...",
    "edit_file": "✏️ Редактирую файл...",
    "search_files": "🔍 Ищу в файлах...",
    "memory_store": "💾 Запоминаю...",
    "memory_recall": "💾 Вспоминаю...",
    "dispatch_subagent": "🤖 Делегирую субагенту...",
}


class StatusMessageSession:
    """Manage a temporary Telegram status message for one request.

    Usage::

        session = StatusMessageSession(bot=bot, source_message=msg, chat_id=chat_id)
        await session.start()
        try:
            result = await agent_loop.run(
                ...,
                on_tool_start=session.mark_tool_start,
            )
        finally:
            await session.close()
    """

    def __init__(
        self,
        *,
        bot: Any,
        source_message: Any,
        chat_id: int,
        update_interval_seconds: float = 2.0,
        typing_heartbeat_seconds: float = 4.0,
        delete_on_finish: bool = True,
        max_updates_per_request: int = 8,
        initial_text: str = "⏳ В обработке...",
    ) -> None:
        self._bot = bot
        self._source_message = source_message
        self._chat_id = chat_id
        self._update_interval_seconds = max(0.5, update_interval_seconds)
        self._typing_heartbeat_seconds = max(1.0, typing_heartbeat_seconds)
        self._delete_on_finish = delete_on_finish
        self._max_updates_per_request = max(0, max_updates_per_request)
        self._current_text = initial_text
        self._desired_text = initial_text
        self._started_at = time.monotonic()
        self._last_update_at = 0.0
        self._last_typing_at = 0.0
        self._update_count = 0
        self._closed = False
        self._message: Any | None = None
        self._worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Send the initial status message as a reply and start the background worker."""
        self._message = await self._source_message.reply_text(self._current_text)
        self._worker_task = asyncio.create_task(self._run())

    async def start_standalone(self) -> None:
        """Send a new status message (not a reply) and start the background worker.

        Use this when there's no source message to reply to — e.g. in ``runner.py``
        where we send the progress message directly via ``bot.send_message``.
        """
        self._message = await self._bot.send_message(chat_id=self._chat_id, text=self._current_text)
        self._worker_task = asyncio.create_task(self._run())

    def mark_tool_start(self, tool_name: str) -> None:
        """Update desired status from a tool execution start.

        This is called synchronously from AgentLoop via ``on_tool_start`` callback.
        """
        friendly = _TOOL_STATUS_MAP.get(tool_name, "⚙️ Выполняю действие...")
        self._set_desired_text(friendly)

    async def close(self) -> None:
        """Stop background work and clean up the temporary message."""
        self._closed = True
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

        if self._message is None:
            return

        try:
            if self._delete_on_finish:
                await self._message.delete()
            else:
                await self._message.edit_text("✅ Готово...")
        except Exception as exc:
            logger.debug("Failed to finalize progress status message: %s", exc)

    def _set_desired_text(self, text: str) -> None:
        """Set a desired text that the worker will send when allowed."""
        if self._closed:
            return
        self._desired_text = text

    async def _run(self) -> None:
        """Maintain typing heartbeats and throttled status updates."""
        while not self._closed:
            now = time.monotonic()
            await self._maybe_send_typing(now)
            self._apply_timed_default_status(now)
            await self._maybe_edit_status(now)
            await asyncio.sleep(0.25)

    async def _maybe_send_typing(self, now: float) -> None:
        if now - self._last_typing_at < self._typing_heartbeat_seconds:
            return
        self._last_typing_at = now
        with contextlib.suppress(Exception):
            await self._bot.send_chat_action(chat_id=self._chat_id, action="typing")

    def _apply_timed_default_status(self, now: float) -> None:
        if self._desired_text != "⏳ В обработке...":
            return
        if now - self._started_at >= 2.0:
            self._desired_text = "🤔 Думаю..."

    async def _maybe_edit_status(self, now: float) -> None:
        if self._message is None or self._desired_text == self._current_text:
            return
        if self._update_count >= self._max_updates_per_request:
            return
        if now - self._last_update_at < self._update_interval_seconds:
            return

        try:
            await self._message.edit_text(self._desired_text)
        except Exception as exc:
            error_str = str(exc).lower()
            if "message is not modified" not in error_str:
                logger.debug("Failed to update progress status message: %s", exc)
            return

        self._current_text = self._desired_text
        self._last_update_at = now
        self._update_count += 1
