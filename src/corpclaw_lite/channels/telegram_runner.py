import argparse
import asyncio
import logging
import sys

from corpclaw_lite.channels.telegram_channel import TelegramChannel


# This is a placeholder for the actual agent routing
async def handle_telegram_message(telegram_id: str, message: str) -> None:
    print(f"Received from {telegram_id}: {message}")
    # Integration with AgentLoop happens here

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-token", type=str, required=True, help="Telegram Bot Token")
    args = parser.parse_args()

    channel = TelegramChannel(token=args.telegram_token, message_handler=handle_telegram_message)
    
    try:
        await channel.start()
        # Keep process alive
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        await channel.stop()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
