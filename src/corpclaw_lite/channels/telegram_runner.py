import asyncio
import logging
import sys

from corpclaw_lite.channels.telegram_channel import TelegramChannel

logger = logging.getLogger(__name__)


async def _handle_message(telegram_id: str, message: str) -> str:
    """Default message handler stub — returns echo until AgentLoop is wired in."""
    logger.info("Message from %s: %s", telegram_id, message)
    return f"[echo] {message}"


async def run_telegram_bot(token: str) -> None:
    """Start the Telegram bot and run until interrupted."""
    channel = TelegramChannel(token=token, message_handler=_handle_message)
    try:
        await channel.start()
        # Keep process alive until KeyboardInterrupt
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await channel.stop()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-token", type=str, required=True)
    args = parser.parse_args()
    asyncio.run(run_telegram_bot(args.telegram_token))
