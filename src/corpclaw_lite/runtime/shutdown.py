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
import contextlib
import logging
import signal
from collections.abc import Coroutine
from typing import Any

__all__ = [
    "install_signal_handlers",
    "run_with_graceful_shutdown",
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


async def run_with_graceful_shutdown(
    main_coro: Coroutine[Any, Any, None],
    cleanup_coro: Coroutine[Any, Any, None] | None = None,
    *,
    timeout: float = 10.0,
) -> None:
    """Run *main_coro*, then *cleanup_coro* on SIGINT/SIGTERM.

    Args:
        main_coro:    The primary coroutine (runs until signal or natural exit).
        cleanup_coro: Optional teardown coroutine (containers, channels…).
                      Always awaited, even if main raised an exception.
        timeout:      Seconds to wait for cleanup before forcing exit.
    """
    shutdown_event = asyncio.Event()
    install_signal_handlers(shutdown_event)

    main_task = asyncio.create_task(main_coro)

    # Wait for either: main finishes naturally OR shutdown signal
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        [main_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel what's still running
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # If main raised (and wasn't cancelled), propagate the exception
    if main_task in done and not main_task.cancelled():
        exc = main_task.exception()
        if exc is not None:
            logger.error("Main coroutine raised: %s", exc)

    # Always run cleanup
    if cleanup_coro is not None:
        logger.info("Running shutdown cleanup…")
        try:
            await asyncio.wait_for(cleanup_coro, timeout=timeout)
        except TimeoutError:
            logger.warning("Cleanup timed out after %.1fs — forcing exit", timeout)
        except Exception as e:
            logger.error("Cleanup error: %s", e)

    logger.info("Shutdown complete.")
