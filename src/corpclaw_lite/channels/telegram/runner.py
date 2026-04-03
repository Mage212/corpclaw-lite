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
from typing import Any

from corpclaw_lite.agent.factory import PROJECT_ROOT, build_agent_stack
from corpclaw_lite.runtime.shutdown import install_signal_handlers

__all__ = [
    "run_telegram_bot",
]

logger = logging.getLogger(__name__)


async def run_telegram_bot(token: str) -> None:
    """Start the Telegram bot and run until interrupted."""
    from corpclaw_lite.channels.telegram.admin_notifier import AdminNotifier
    from corpclaw_lite.channels.telegram.channel import TelegramChannel
    from corpclaw_lite.channels.telegram.progress import StatusMessageSession
    from corpclaw_lite.channels.telegram.rate_limit import RateLimiter
    from corpclaw_lite.config.bootstrap import BootstrapLoader
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.extensions.skills.registry import SkillRegistry
    from corpclaw_lite.extensions.skills.watcher import SkillHotReloader
    from corpclaw_lite.logging.agent_logger import AgentLogger, setup_logging
    from corpclaw_lite.users.models import User

    full_settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    log_cfg = full_settings.logging
    setup_logging(
        log_dir=PROJECT_ROOT / log_cfg.log_dir,
        level=log_cfg.level,
        console_level=log_cfg.console_level,
    )
    agent_activity_logger = AgentLogger(log_dir=PROJECT_ROOT / log_cfg.log_dir)

    agent_loop, user_manager, tool_registry, mcp_manager, container_manager = build_agent_stack()
    from corpclaw_lite.container.manager import ContainerManagerError

    bootstrap = BootstrapLoader(PROJECT_ROOT / "config" / "bootstrap")
    tg_settings = full_settings.telegram
    rate_limiter = RateLimiter(max_per_minute=tg_settings.rate_limit_per_minute)

    # ── MCP Servers ────────────────────────────────────────────────
    if mcp_manager is not None:
        try:
            mcp_count = await mcp_manager.connect_all(tool_registry)
            logger.info("MCP: connected, %d tools registered", mcp_count)
        except Exception as e:
            logger.error("MCP: failed to connect — %s (continuing without MCP tools)", e)

    # ── Skills ────────────────────────────────────────────────
    skill_registry = SkillRegistry()
    skills_dir = PROJECT_ROOT / "skills"
    if skills_dir.exists():
        skill_registry.load_directory(skills_dir)

    # ── Plugins ───────────────────────────────────────────────────────────────
    from corpclaw_lite.extensions.plugins.registry import PluginRegistry

    plugin_registry = PluginRegistry()
    plugins_dir = PROJECT_ROOT / "plugins"
    if plugins_dir.exists():
        plugin_registry.load_directory(plugins_dir)
        for plugin in plugin_registry.list_all():
            for tool in plugin.tools:
                try:
                    tool_registry.register(tool)
                    logger.info(
                        "Plugin '%s': registered tool '%s'", plugin.manifest.name, tool.name
                    )
                except ValueError:
                    logger.warning(
                        "Plugin '%s': tool '%s' conflicts with an existing tool, skipping.",
                        plugin.manifest.name,
                        tool.name,
                    )

    # Seed whitelist from config into persistent JSON
    if tg_settings.whitelist:
        user_manager.seed_whitelist(tg_settings.whitelist, tg_settings.default_department)

    # Placeholder for admin notifier — set after channel.start()
    admin_notifier: AdminNotifier | None = None
    _background_tasks: set[asyncio.Task[Any]] = set()

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
        user = await user_manager.async_get_by_telegram_id(tid)
        if not user:
            dept = user_manager.get_whitelist_department(tid)
            user = await user_manager.async_create_user(telegram_id=tid, department=dept)
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
        bot = channel.bot
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

        # ── Container isolation guard ───────────────────────────────────────
        # If container isolation is enabled, ensure the sandbox is running.
        # Hard block: agent will NOT run without a healthy container.
        if container_manager is not None:
            try:
                container_manager.ensure_running(tid)
            except ContainerManagerError as e:
                logger.error("Container failed for user %d: %s", tid, e)
                await channel.send_message(
                    user,
                    "⚠️ Изолированная рабочая среда временно недоступна.\n"
                    "Пожалуйста, повторите попытку позже или обратитесь к администратору.",
                )
                return

        # ── Agent execution ───────────────────────────────────────────────
        try:
            base_prompt = bootstrap.get_system_prompt()
            dept_prompt = bootstrap.get_department_prompt(user.department)
            user_ctx = f"You are talking to {user.name} from the {user.department} department."
            parts_list = [p for p in [base_prompt, dept_prompt, user_ctx] if p]
            system_prompt: str | None = "\n\n".join(parts_list) if parts_list else None

            # Inject allowed skill instructions into system prompt
            allowed_skills = skill_registry.get_allowed_skills(user)
            plugin_skills = [
                p.skill for p in plugin_registry.get_allowed_plugins(user) if p.skill is not None
            ]
            from corpclaw_lite.agent.prompt import build_skill_block

            skill_block = build_skill_block(allowed_skills, plugin_skills)
            if skill_block:
                system_prompt = (system_prompt or "") + skill_block

            async def approval_cb(action: str, details: str) -> bool:
                return await channel.request_approval(user, action, details)

            reply, run_stats = await agent_loop.run(
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
            run_stats = None
            # Notify admins
            if admin_notifier is not None:
                error_summary = (
                    f"🔴 Agent error\n"
                    f"User: {tid} ({user.name})\n"
                    f"Error: {type(e).__name__}: {str(e)[:200]}"
                )
                _task = asyncio.create_task(admin_notifier.notify(error_summary))
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)
        finally:
            if status_session is not None:
                await status_session.close()

        # ── Structured activity log ────────────────────────────────────────
        agent_activity_logger.log_request(
            user_id=str(tid),
            department=user.department,
            message_preview=message[:100],
            duration_ms=run_stats.duration_ms if run_stats is not None else 0.0,
            tools_used=run_stats.tools_used if run_stats is not None else [],
            status=run_stats.status if run_stats is not None else "error",
            error=run_stats.error if run_stats is not None else None,
        )

        await channel.send_message(user, reply)

    # ── Build channel ─────────────────────────────────────────────────────
    channel = TelegramChannel(
        token=token,
        message_handler=_handle_and_reply,
        workspace_base=tg_settings.workspace_base,
        tool_registry=tool_registry,
        memory=agent_loop.memory,
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

    # ── Health endpoint ───────────────────────────────────────────────────────
    _health_runner: Any = None
    try:
        import importlib.util

        if importlib.util.find_spec("aiohttp") is None:
            raise ImportError("aiohttp missing")

        from corpclaw_lite.logging import health

        _health_runner = await health.run_health_server()
        logger.info("Health endpoint started on :8080/health")
    except ImportError:
        logger.info("aiohttp not installed — health endpoint disabled")

    # ── Skill Hot-Reloader ────────────────────────────────────────────────
    reloader = SkillHotReloader(skills_dir, skill_registry)
    reloader.start()
    logger.info("Skill hot-reloader started watching %s", skills_dir)

    # ── MCP Hot-Reloader ────────────────────────────────────────────
    mcp_reloader: MCPHotReloader | None = None
    if mcp_manager is not None:
        from corpclaw_lite.extensions.mcp.watcher import MCPHotReloader

        mcp_cfg_path = PROJECT_ROOT / "config" / "mcp_servers.yaml"
        mcp_reloader = MCPHotReloader(mcp_cfg_path, mcp_manager, tool_registry)
        mcp_reloader.start()
        logger.info("MCP hot-reloader started watching %s", mcp_cfg_path)

    # ── Plugin Hot-Reloader ──────────────────────────────────────────────────
    from corpclaw_lite.extensions.plugins.watcher import PluginHotReloader

    plugin_reloader = PluginHotReloader(plugins_dir, plugin_registry, tool_registry, skill_registry)
    plugin_reloader.start()
    logger.info("Plugin hot-reloader started watching %s", plugins_dir)

    # ── Start ─────────────────────────────────────────────────────────────
    logger.info("Starting Telegram bot...")
    cleanup_task: asyncio.Task[None] | None = None
    shutdown_event = asyncio.Event()
    install_signal_handlers(shutdown_event)
    try:
        await channel.start()

        # Wire admin notifier after channel starts (needs bot instance)
        if tg_settings.admin_ids and channel.app:
            admin_notifier = AdminNotifier(
                bot=channel.app.bot,
                admin_ids=tg_settings.admin_ids,
            )
            logger.info("Admin notifier active for %d admin(s)", len(tg_settings.admin_ids))

        cleanup_task = asyncio.create_task(_rate_limit_cleanup_loop())
        # Wait until a signal fires
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        # Each cleanup step is individually guarded so that a failure
        # in one step never prevents subsequent steps from running.
        # This is the #1 cause of ghost processes: an exception in
        # early cleanup skips later cleanup (MCP, containers, etc.).
        if _health_runner is not None:
            try:
                await _health_runner.cleanup()
            except Exception as e:
                logger.warning("Health server cleanup failed: %s", e)
        if cleanup_task is not None:
            cleanup_task.cancel()
        for task in _background_tasks:
            task.cancel()
        try:
            reloader.stop()
        except Exception as e:
            logger.warning("SkillHotReloader stop failed: %s", e)
        if mcp_reloader is not None:
            try:
                mcp_reloader.stop()
            except Exception as e:
                logger.warning("MCPHotReloader stop failed: %s", e)
        try:
            plugin_reloader.stop()
        except Exception as e:
            logger.warning("PluginHotReloader stop failed: %s", e)
        try:
            await channel.stop()
        except Exception as e:
            logger.warning("Channel stop failed: %s", e)
        # Disconnect MCP servers
        if mcp_manager is not None:
            try:
                await mcp_manager.disconnect_all()
                logger.info("MCP servers disconnected.")
            except Exception as e:
                logger.warning("MCP disconnect failed: %s", e)
        # Stop all per-user Docker containers so no ghost containers remain
        if container_manager is not None:
            try:
                active = await container_manager.list_active()
                for cname in active:
                    try:
                        container_manager.stop_by_name(cname)
                    except Exception as e:
                        logger.warning("Could not stop container %s: %s", cname, e)
            except Exception as e:
                logger.warning("Container cleanup failed: %s", e)
        logger.info("Telegram bot stopped cleanly.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-token", type=str, required=True)
    args = parser.parse_args()
    asyncio.run(run_telegram_bot(args.telegram_token))
