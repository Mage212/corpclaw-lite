"""B-120 / DC-032: proactive user delivery (system session + optional live sinks).

Persists into the durable per-user system session (B-119) and best-effort pushes
to registered process-local sinks (WebSocket broadcast and/or Telegram bot).
AdminNotifier remains separate (admin error alerts only).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from corpclaw_lite.channels.web.chat_store import WebChatStore
from corpclaw_lite.logging.trace import log_event
from corpclaw_lite.users.models import User

__all__ = [
    "NotifyResult",
    "TelegramBotLike",
    "UserNotifier",
    "WebBroadcastFn",
]

logger = logging.getLogger(__name__)

_MAX_NOTIFY_CHARS = 20_000
_DEFAULT_SOURCE = "manual"

WebBroadcastFn = Callable[[int, dict[str, object]], Awaitable[None]]


@runtime_checkable
class TelegramBotLike(Protocol):
    """Minimal bot surface for proactive Telegram delivery."""

    async def send_message(self, chat_id: int, text: str) -> Any: ...


@dataclass(slots=True)
class NotifyResult:
    """Outcome of :meth:`UserNotifier.notify`."""

    ok: bool
    session_id: int | None = None
    message_id: int | None = None
    web_pushed: bool = False
    telegram_sent: bool = False
    errors: list[str] = field(default_factory=lambda: [])

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "web_pushed": self.web_pushed,
            "telegram_sent": self.telegram_sent,
            "errors": list(self.errors),
        }


class UserNotifier:
    """Deliver a proactive message to a user without an inbound chat turn.

    Sinks are process-local. Dual-process deploys (web + telegram separate)
    always share the DB; live push only works in the process that registered
    the corresponding sink.
    """

    def __init__(self, chat_store: WebChatStore) -> None:
        self._chat_store = chat_store
        self._web_broadcast: WebBroadcastFn | None = None
        self._telegram_bot: TelegramBotLike | None = None

    def register_web_broadcast(self, fn: WebBroadcastFn) -> None:
        """Register a WebSocket (or similar) fan-out for online web clients."""
        self._web_broadcast = fn

    def register_telegram_bot(self, bot: TelegramBotLike) -> None:
        """Register a Telegram bot for proactive DMs."""
        self._telegram_bot = bot

    async def notify(
        self,
        user: User,
        text: str,
        *,
        source: str = _DEFAULT_SOURCE,
        title: str | None = None,
        persist: bool = True,
        extra_metadata: dict[str, object] | None = None,
    ) -> NotifyResult:
        """Persist (optional) to system session, then best-effort push sinks.

        When *persist* is False (headless already wrote the assistant row), only
        resolve the system session id and push live sinks — no second append.

        *extra_metadata* is merged into the stored/WS message metadata after base
        keys. Callers cannot override ``proactive`` or ``source`` via extra.
        """
        body = (text or "").strip()
        if not body:
            return NotifyResult(ok=False, errors=["empty_text"])
        if len(body) > _MAX_NOTIFY_CHARS:
            body = body[:_MAX_NOTIFY_CHARS]

        safe_source = (source or _DEFAULT_SOURCE).strip() or _DEFAULT_SOURCE
        safe_title = title.strip() if isinstance(title, str) and title.strip() else None
        errors: list[str] = []
        session_id: int | None = None
        message_id: int | None = None
        created_at = ""
        metadata: dict[str, object] = {
            "proactive": True,
            "source": safe_source,
        }
        if safe_title is not None:
            metadata["title"] = safe_title
        if extra_metadata:
            for key, value in extra_metadata.items():
                if key in {"proactive", "source"}:
                    continue
                metadata[key] = value

        try:
            session_id = await self._chat_store.ensure_system_session(user.memory_key())
        except Exception as exc:
            logger.warning(
                "user_notifier ensure_system_session failed user=%s: %s",
                user.id,
                exc,
            )
            return NotifyResult(ok=False, errors=[f"persist:{exc}"])

        if persist:
            try:
                msg = await self._chat_store.append_message(
                    user_id=user.memory_key(),
                    role="assistant",
                    content=body,
                    session_id=session_id,
                    metadata=metadata,
                )
                message_id = msg.id
                created_at = msg.created_at
            except Exception as exc:
                logger.warning(
                    "user_notifier append_message failed user=%s: %s",
                    user.id,
                    exc,
                )
                return NotifyResult(
                    ok=False,
                    session_id=session_id,
                    errors=[f"persist:{exc}"],
                )

        message_payload: dict[str, object] = {
            "id": f"db_{message_id}" if message_id is not None else "proactive_ephemeral",
            "role": "assistant",
            "text": body,
            "session_id": session_id,
            "created_at": created_at,
            "metadata": metadata,
        }
        if message_id is not None:
            message_payload["db_id"] = message_id

        # D-084: parallel multichannel delivery — web and Telegram are peers.
        # Persist above runs first; live sinks fan out concurrently.
        web_pushed, telegram_sent, sink_errors = await self._push_sinks_parallel(
            user=user,
            body=body,
            session_id=session_id,
            source=safe_source,
            title=safe_title,
            message_payload=message_payload,
        )
        errors.extend(sink_errors)

        log_event(
            "proactive_notified",
            "",
            user_id=user.id,
            session_id=session_id,
            source=safe_source,
            persist=persist,
            web_pushed=web_pushed,
            telegram_sent=telegram_sent,
            message_id=message_id,
            error_count=len(errors),
        )

        return NotifyResult(
            ok=True,
            session_id=session_id,
            message_id=message_id,
            web_pushed=web_pushed,
            telegram_sent=telegram_sent,
            errors=errors,
        )

    async def _push_sinks_parallel(
        self,
        *,
        user: User,
        body: str,
        session_id: int | None,
        source: str,
        title: str | None,
        message_payload: dict[str, object],
    ) -> tuple[bool, bool, list[str]]:
        """Best-effort concurrent push to web + Telegram sinks."""

        async def push_web() -> tuple[bool, str | None]:
            if self._web_broadcast is None:
                return False, None
            try:
                await self._web_broadcast(
                    user.id,
                    {
                        "type": "proactive_message",
                        "session_id": session_id,
                        "source": source,
                        "title": title,
                        "message": message_payload,
                    },
                )
                await self._web_broadcast(user.id, {"type": "chat_list_changed"})
                return True, None
            except Exception as exc:
                logger.warning(
                    "user_notifier web broadcast failed user=%s: %s",
                    user.id,
                    exc,
                )
                return False, f"web:{exc}"

        async def push_telegram() -> tuple[bool, str | None]:
            if self._telegram_bot is None or user.telegram_id is None:
                return False, None
            try:
                await self._telegram_bot.send_message(chat_id=user.telegram_id, text=body)
                return True, None
            except Exception as exc:
                logger.warning(
                    "user_notifier telegram send failed user=%s tg=%s: %s",
                    user.id,
                    user.telegram_id,
                    exc,
                )
                return False, f"telegram:{exc}"

        web_result, tg_result = await asyncio.gather(push_web(), push_telegram())
        web_pushed, web_err = web_result
        telegram_sent, tg_err = tg_result
        sink_errors: list[str] = []
        if web_err is not None:
            sink_errors.append(web_err)
        if tg_err is not None:
            sink_errors.append(tg_err)
        return web_pushed, telegram_sent, sink_errors
