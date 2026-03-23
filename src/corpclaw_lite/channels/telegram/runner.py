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
from pathlib import Path

from corpclaw_lite.agent.factory import build_agent_stack

logger = logging.getLogger(__name__)


async def run_telegram_bot(token: str) -> None:
    """Start the Telegram bot and run until interrupted."""
    from corpclaw_lite.channels.telegram.channel import TelegramChannel
    from corpclaw_lite.config.bootstrap import BootstrapLoader
    from corpclaw_lite.users.models import User

    agent_loop, user_manager, tool_registry = build_agent_stack()
    bootstrap = BootstrapLoader(Path("config/bootstrap"))

    async def _handle_message(telegram_id: str, message: str) -> str:
        tid = int(telegram_id)
        user = user_manager.get_by_telegram_id(tid)
        if not user:
            user = user_manager.create_user(telegram_id=tid, department="default")
            logger.info("Auto-registered new user telegram_id=%d", tid)

        # Build system prompt: bootstrap base + department context + user context
        base_prompt = bootstrap.get_system_prompt()
        dept_prompt = bootstrap.get_department_prompt(user.department)
        user_context = f"You are talking to {user.name} from the {user.department} department."

        parts = [p for p in [base_prompt, dept_prompt, user_context] if p]
        system_prompt: str | None = "\n\n".join(parts) if parts else None

        # Per-request closure — captures this user and channel; thread-safe (no shared mutation)
        async def approval_cb(action: str, details: str) -> bool:
            return await channel.request_approval(user, action, details)

        try:
            return await agent_loop.run(
                user, message, system_prompt=system_prompt, approval_callback=approval_cb
            )
        except Exception as e:
            logger.error("AgentLoop error for user %d: %s", tid, e)
            return f"Sorry, I encountered an error: {e}"

    channel = TelegramChannel(token=token, message_handler=_handle_message)

    # ── SendFile tool (needs channel reference) ───────────────────────────────
    from corpclaw_lite.extensions.tools.builtin.send_file import SendFileTool

    async def _send_file_cb(path: Path, user: User, caption: str) -> str:
        await channel.send_file(user, path, caption)
        return f"File '{path.name}' sent to user."

    tool_registry.register(SendFileTool(_send_file_cb))

    # Wire send_message back so agent can reply; per-request closure captures user + channel
    async def _handle_and_reply(telegram_id: str, message: str) -> None:
        tid = int(telegram_id)
        user = user_manager.get_by_telegram_id(tid) or User(
            id=0, name=f"user_{tid}", department="default", telegram_id=tid
        )
        reply = await _handle_message(telegram_id, message)
        await channel.send_message(user, reply)

    # Replace handler with the reply-capable version
    channel._on_message = _handle_and_reply  # type: ignore[assignment]

    # ── Health endpoint ────────────────────────────────────────────────────────
    try:
        from corpclaw_lite.logging import health

        asyncio.create_task(health.run_health_server())
        logger.info("Health endpoint started on :8080/health")
    except ImportError:
        logger.info("aiohttp not installed — health endpoint disabled")

    # ── Skill Hot-Reloader ────────────────────────────────────────────────────
    from corpclaw_lite.extensions.skills.registry import SkillRegistry
    from corpclaw_lite.extensions.skills.watcher import SkillHotReloader

    skill_registry = SkillRegistry()
    skills_dir = Path("skills")
    if skills_dir.exists():
        skill_registry.load_directory(skills_dir)
    reloader = SkillHotReloader(skills_dir, skill_registry)
    reloader.start()
    logger.info("Skill hot-reloader started watching %s", skills_dir)

    logger.info("Starting Telegram bot...")
    try:
        await channel.start()
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        reloader.stop()
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
