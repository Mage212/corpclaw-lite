"""
Telegram bot runner — thin wrapper around TelegramBotOrchestrator.

Environment variables required:
    TELEGRAM_BOT_TOKEN      — Telegram bot token
    CORPCLAW_IPC_SECRET     — IPC HMAC secret (fail-fast if absent)
"""

from __future__ import annotations

import asyncio
import sys

from corpclaw_lite.channels.telegram.orchestrator import TelegramBotOrchestrator
from corpclaw_lite.config.loader import load_settings
from corpclaw_lite.paths import PROJECT_ROOT

__all__ = [
    "run_telegram_bot",
]


async def run_telegram_bot(token: str) -> None:
    """Start the Telegram bot and run until interrupted."""
    settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    orchestrator = TelegramBotOrchestrator(token, settings)

    try:
        await orchestrator.start()
        await orchestrator.run_until_shutdown()
    except asyncio.CancelledError:
        pass
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-token", type=str, required=True)
    args = parser.parse_args()
    asyncio.run(run_telegram_bot(args.telegram_token))
