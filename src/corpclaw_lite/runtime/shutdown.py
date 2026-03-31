"""Centralised graceful shutdown for all CorpClaw Lite entry points.

Usage pattern (both CLI and Telegram runner):

    async def _my_main() -> None:
        ...   # your app logic — runs until cancelled

    async def _cleanup() -> None:
        ...   # stop containers, flush logs, etc.

    asyncio.run(run_with_graceful_shutdown(_my_main(), _cleanup))

SIGINT (Ctrl+C) and SIGTERM (systemd / docker stop) both trigger shutdown.
The cleanup coroutine is **guaranteed** to run even if main raises.
"""

from __future__ import annotations

import asyncio
import logging
import signal

__all__ = [
    "install_signal_handlers",
]

logger = logging.getLogger(__name__)


def install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """Register SIGINT + SIGTERM handlers that set *shutdown_event*.

    Must be called from inside a running event loop (i.e. inside an ``async``
    function) because ``loop.add_signal_handler`` is loop-bound.

    On Windows, signal handling via the event loop is not supported;  we fall
    back to the default Python handler (Ctrl+C still works via KeyboardInterrupt).
    """
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown_event.set)
        logger.debug("Signal handlers installed for SIGINT + SIGTERM")
    except (NotImplementedError, AttributeError):
        # Windows / environments without signal support
        logger.debug("Signal handlers not available on this platform (Windows?)")
