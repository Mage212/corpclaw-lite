"""
Telegram bot runner — bootstraps the full agent stack and starts polling.

Environment variables required:
    TELEGRAM_BOT_TOKEN      — Telegram bot token
    CORPCLAW_IPC_SECRET     — IPC HMAC secret (fail-fast if absent)
    ANTHROPIC_API_KEY       — (optional) Anthropic key; falls back to OpenAI/local
"""

from __future__ import annotations

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


def _build_agent_loop() -> tuple[object, object]:
    """Build and return (AgentLoop, UserManager) using env/config settings."""
    import os

    from corpclaw_lite.agent.loop import AgentLoop
    from corpclaw_lite.config.settings import AgentSettings, LLMSettings, ProviderSettings
    from corpclaw_lite.extensions.tools.builtin.files import (
        EditFileTool,
        ListFilesTool,
        ReadFileTool,
        SearchFilesTool,
        WriteFileTool,
    )
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from corpclaw_lite.users.manager import UserManager

    # ── Provider ──────────────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        provider = AnthropicProvider(
            ProviderSettings(type="anthropic", model="claude-3-haiku-20240307", api_key=api_key)
        )
    else:
        base_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
        model = os.environ.get("OPENAI_MODEL", "qwen2.5:7b")
        from corpclaw_lite.llm.openai import OpenAIProvider

        provider = OpenAIProvider(
            ProviderSettings(type="openai", model=model, api_key="ollama", base_url=base_url)
        )
        logger.info("Using local LLM at %s model=%s", base_url, model)

    # ── Tools ─────────────────────────────────────────────────────────────────
    registry = ToolRegistry()
    builtin_tools = [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
    ]
    for tool in builtin_tools:
        registry.register(tool)

    # ── Memory ────────────────────────────────────────────────────────────────
    memory = SQLiteMemory()

    # ── Settings ──────────────────────────────────────────────────────────────
    agent_settings = AgentSettings()
    _ = LLMSettings()  # imported for completeness; not passed directly

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        settings=agent_settings,
        memory=memory,
    )
    user_manager = UserManager()
    return loop, user_manager


async def run_telegram_bot(token: str) -> None:
    """Start the Telegram bot and run until interrupted."""
    from corpclaw_lite.agent.loop import AgentLoop
    from corpclaw_lite.channels.telegram_channel import TelegramChannel
    from corpclaw_lite.users.manager import UserManager

    agent_loop, user_manager = _build_agent_loop()
    assert isinstance(agent_loop, AgentLoop)
    assert isinstance(user_manager, UserManager)

    async def _handle_message(telegram_id: str, message: str) -> str:
        tid = int(telegram_id)
        user = user_manager.get_by_telegram_id(tid)
        if not user:
            user = user_manager.create_user(telegram_id=tid, department="default")
            logger.info("Auto-registered new user telegram_id=%d", tid)

        try:
            return await agent_loop.run(user, message)
        except Exception as e:
            logger.error("AgentLoop error for user %d: %s", tid, e)
            return f"Sorry, I encountered an error: {e}"

    channel = TelegramChannel(token=token, message_handler=_handle_message)

    # Wire send_message back into the handler so agent can reply
    async def _handle_and_reply(telegram_id: str, message: str) -> None:
        from corpclaw_lite.users.models import User

        tid = int(telegram_id)
        user = user_manager.get_by_telegram_id(tid) or User(
            id=0, name=f"user_{tid}", department="default", telegram_id=tid
        )
        reply = await _handle_message(telegram_id, message)
        await channel.send_message(user, reply)

    # Replace handler with the reply-capable version
    channel._on_message = _handle_and_reply  # type: ignore[assignment]

    logger.info("Starting Telegram bot...")
    try:
        await channel.start()
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await channel.stop()
        logger.info("Telegram bot stopped.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-token", type=str, required=True)
    args = parser.parse_args()
    asyncio.run(run_telegram_bot(args.telegram_token))
