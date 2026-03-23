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
    from corpclaw_lite.channels.telegram.admin_notifier import AdminNotifier
    from corpclaw_lite.channels.telegram.channel import TelegramChannel
    from corpclaw_lite.channels.telegram.progress import StatusMessageSession
    from corpclaw_lite.channels.telegram.rate_limit import RateLimiter
    from corpclaw_lite.config.bootstrap import BootstrapLoader
    from corpclaw_lite.config.settings import TelegramSettings
    from corpclaw_lite.users.models import User

    agent_loop, user_manager, tool_registry = build_agent_stack()
    bootstrap = BootstrapLoader(Path("config/bootstrap"))
    tg_settings = TelegramSettings()
    rate_limiter = RateLimiter(max_per_minute=tg_settings.rate_limit_per_minute)

    # Seed whitelist from config into persistent JSON
    if tg_settings.whitelist:
        user_manager.seed_whitelist(tg_settings.whitelist, tg_settings.default_department)

    # Placeholder for admin notifier — set after channel.start()
    admin_notifier: AdminNotifier | None = None

    # ── Message handler (called by channel for text/upload/photo) ─────────
    async def _handle_and_reply(telegram_id: str, message: str, mode: str = "execute") -> None:
        tid = int(telegram_id)

        # ── Access control ────────────────────────────────────────────────
        if user_manager.is_session_revoked(tid):
            logger.debug("Ignoring message from revoked session telegram_id=%d", tid)
            return

        if not user_manager.is_allowed(tid):
            temp_user = User(id=0, name=f"user_{tid}", department="default", telegram_id=tid)
            await channel.send_message(temp_user, "⛔ У вас нет доступа к этому боту.")
            return

        # ── Get or register user ──────────────────────────────────────────
        user = user_manager.get_by_telegram_id(tid)
        if not user:
            dept = user_manager.get_whitelist_department(tid)
            user = user_manager.create_user(telegram_id=tid, department=dept)
            logger.info("Auto-registered user telegram_id=%d (dept=%s)", tid, dept)

        # ── Rate limiting ─────────────────────────────────────────────────
        allowed = await rate_limiter.check(tid)
        if not allowed:
            await channel.send_message(
                user,
                "⚠️ Слишком много сообщений. Подождите минуту и попробуйте снова.",
            )
            return

        # ── Progress indicator ────────────────────────────────────────────
        bot = channel._app.bot if channel._app else None  # type: ignore[union-attr]
        status_session: StatusMessageSession | None = None

        if bot is not None:
            try:
                status_session = StatusMessageSession(
                    bot=bot,
                    source_message=None,  # type: ignore[arg-type]
                    chat_id=tid,
                )
                await status_session.start_standalone()
            except Exception as exc:
                logger.warning("Failed to start progress indicator: %s", exc)
                status_session = None

        # ── Agent execution ───────────────────────────────────────────────
        try:
            base_prompt = bootstrap.get_system_prompt()
            dept_prompt = bootstrap.get_department_prompt(user.department)
            user_ctx = f"You are talking to {user.name} from the {user.department} department."
            parts_list = [p for p in [base_prompt, dept_prompt, user_ctx] if p]
            system_prompt: str | None = "\n\n".join(parts_list) if parts_list else None

            async def approval_cb(action: str, details: str) -> bool:
                return await channel.request_approval(user, action, details)

            reply = await agent_loop.run(
                user,
                message,
                system_prompt=system_prompt,
                approval_callback=approval_cb,
                on_tool_start=(
                    status_session.mark_tool_start if status_session is not None else None
                ),
                tools_enabled=(mode == "execute"),
            )
        except Exception as e:
            logger.error("AgentLoop error for user %d: %s", tid, e)
            reply = f"❌ Произошла ошибка: {e}"
            # Notify admins
            if admin_notifier is not None:
                error_summary = (
                    f"🔴 Agent error\n"
                    f"User: {tid} ({user.name})\n"
                    f"Error: {type(e).__name__}: {str(e)[:200]}"
                )
                asyncio.create_task(admin_notifier.notify(error_summary))
        finally:
            if status_session is not None:
                await status_session.close()

        await channel.send_message(user, reply)

    # ── Build channel ─────────────────────────────────────────────────────
    # Memory is available from build_agent_stack via agent_loop's public interface,
    # but we need it for /new command. Pass it directly.
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    _memory: SQLiteMemory | None = getattr(agent_loop, "_memory", None)
    channel = TelegramChannel(
        token=token,
        message_handler=_handle_and_reply,
        workspace_base=tg_settings.workspace_base,
        tool_registry=tool_registry,
        memory=_memory,
    )

    # ── SendFile tool ─────────────────────────────────────────────────────
    from corpclaw_lite.extensions.tools.builtin.send_file import SendFileTool

    async def _send_file_cb(path: Path, user: User, caption: str) -> str:
        await channel.send_file(user, path, caption)
        return f"File '{path.name}' sent to user."

    tool_registry.register(SendFileTool(_send_file_cb))

    # ── Rate limit cleanup ────────────────────────────────────────────────
    async def _rate_limit_cleanup_loop() -> None:
        while True:
            await asyncio.sleep(300)
            await rate_limiter.cleanup()

    # ── Health endpoint ───────────────────────────────────────────────────
    try:
        from corpclaw_lite.logging import health

        asyncio.create_task(health.run_health_server())
        logger.info("Health endpoint started on :8080/health")
    except ImportError:
        logger.info("aiohttp not installed — health endpoint disabled")

    # ── Skill Hot-Reloader ────────────────────────────────────────────────
    from corpclaw_lite.extensions.skills.registry import SkillRegistry
    from corpclaw_lite.extensions.skills.watcher import SkillHotReloader

    skill_registry = SkillRegistry()
    skills_dir = Path("skills")
    if skills_dir.exists():
        skill_registry.load_directory(skills_dir)
    reloader = SkillHotReloader(skills_dir, skill_registry)
    reloader.start()
    logger.info("Skill hot-reloader started watching %s", skills_dir)

    # ── Start ─────────────────────────────────────────────────────────────
    logger.info("Starting Telegram bot...")
    cleanup_task: asyncio.Task[None] | None = None
    try:
        await channel.start()

        # Wire admin notifier after channel starts (needs bot instance)
        if tg_settings.admin_ids and channel._app:  # type: ignore[union-attr]
            admin_notifier = AdminNotifier(
                bot=channel._app.bot,  # type: ignore[union-attr]
                admin_ids=tg_settings.admin_ids,
            )
            logger.info("Admin notifier active for %d admin(s)", len(tg_settings.admin_ids))

        cleanup_task = asyncio.create_task(_rate_limit_cleanup_loop())
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
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
