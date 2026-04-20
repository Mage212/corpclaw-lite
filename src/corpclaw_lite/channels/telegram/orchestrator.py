"""TelegramBotOrchestrator — orchestrates the full Telegram bot lifecycle.

Extracts the monolithic ``run_telegram_bot()`` closure into a class with
clear separation between startup, message handling, and shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpclaw_lite.agent.factory import build_agent_stack
from corpclaw_lite.channels.telegram.admin_notifier import AdminNotifier
from corpclaw_lite.channels.telegram.channel import TelegramChannel
from corpclaw_lite.channels.telegram.progress import StatusMessageSession
from corpclaw_lite.channels.telegram.rate_limit import RateLimiter
from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.container.manager import ContainerManagerError
from corpclaw_lite.extensions.plugins.watcher import PluginHotReloader
from corpclaw_lite.extensions.skills.watcher import SkillHotReloader
from corpclaw_lite.logging.agent_logger import AgentLogger, setup_logging
from corpclaw_lite.onboarding.engine import OnboardingEngine
from corpclaw_lite.onboarding.finalizer import OnboardingFinalizer
from corpclaw_lite.onboarding.storage import OnboardingStorage
from corpclaw_lite.paths import PROJECT_ROOT
from corpclaw_lite.runtime.shutdown import install_signal_handlers
from corpclaw_lite.users.models import User

if TYPE_CHECKING:
    from corpclaw_lite.agent.factory import AgentStack
    from corpclaw_lite.agent.vision import VisionProcessor
    from corpclaw_lite.config.settings import Settings

__all__ = [
    "TelegramBotOrchestrator",
]

logger = logging.getLogger(__name__)


class TelegramBotOrchestrator:
    """Orchestrates the full Telegram bot: startup, message handling, shutdown."""

    def __init__(self, token: str, settings: Settings) -> None:
        self._token = token
        self._settings = settings

        self._stack: AgentStack | None = None
        self._channel: TelegramChannel | None = None
        self._onboarding_engine: OnboardingEngine | None = None
        self._rate_limiter: RateLimiter | None = None
        self._admin_notifier: AdminNotifier | None = None
        self._bootstrap: BootstrapLoader | None = None
        self._vision_processor: VisionProcessor | None = None

        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._health_runner: Any = None
        self._reloader: SkillHotReloader | None = None
        self._mcp_reloader: Any = None
        self._plugin_reloader: PluginHotReloader | None = None
        self._subagent_reloader: Any = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._shutdown_event = asyncio.Event()
        self._agent_activity_logger: AgentLogger | None = None

    def _require_started(
        self,
    ) -> tuple[AgentStack, TelegramChannel, RateLimiter, BootstrapLoader]:
        """Return all required components, raising RuntimeError if any is missing."""
        if self._stack is None:
            raise RuntimeError("AgentStack not initialized — call start() first")
        if self._channel is None:
            raise RuntimeError("TelegramChannel not initialized")
        if self._rate_limiter is None:
            raise RuntimeError("RateLimiter not initialized")
        if self._bootstrap is None:
            raise RuntimeError("BootstrapLoader not initialized")
        return (
            self._stack,
            self._channel,
            self._rate_limiter,
            self._bootstrap,
        )

    async def start(self) -> None:
        """Build agent stack, wire components, start bot."""
        log_cfg = self._settings.logging
        setup_logging(
            log_dir=PROJECT_ROOT / log_cfg.log_dir,
            level=log_cfg.level,
            console_level=log_cfg.console_level,
        )
        self._agent_activity_logger = AgentLogger(log_dir=PROJECT_ROOT / log_cfg.log_dir)

        stack = build_agent_stack()
        self._stack = stack
        agent_loop = stack.loop
        user_manager = stack.user_manager
        tool_registry = stack.tool_registry
        mcp_manager = stack.mcp_manager

        tg_settings = self._settings.telegram
        self._rate_limiter = RateLimiter(max_per_minute=tg_settings.rate_limit_per_minute)

        self._bootstrap = BootstrapLoader(PROJECT_ROOT / "config" / "bootstrap")

        # MCP Servers
        if mcp_manager is not None:
            try:
                mcp_count = await mcp_manager.connect_all(tool_registry)
                logger.info("MCP: connected, %d tools registered", mcp_count)
            except Exception as e:
                logger.error("MCP: failed to connect — %s (continuing without MCP tools)", e)

        # Skills + Plugins + SkillMatcher — loaded inside build_agent_stack()
        skill_registry = stack.skill_registry
        plugin_registry = stack.plugin_registry

        # Seed whitelist
        if tg_settings.whitelist:
            user_manager.seed_whitelist(tg_settings.whitelist, tg_settings.default_department)

        # Onboarding
        onboarding_storage = OnboardingStorage(db_path=Path("data/users.db"))
        onboarding_finalizer: OnboardingFinalizer | None = None
        if agent_loop.memory is not None:
            onboarding_finalizer = OnboardingFinalizer(
                provider=agent_loop.provider,
                memory=agent_loop.memory,
                bootstrap_users_dir=PROJECT_ROOT / "config" / "bootstrap" / "users",
                user_manager=user_manager,
            )
        if onboarding_finalizer is not None:
            self._onboarding_engine = OnboardingEngine(onboarding_storage, onboarding_finalizer)

        # Vision processor
        from corpclaw_lite.extensions.tools.builtin.image import ReadImageTool as _RIT

        _rit = tool_registry.get("read_image")
        if isinstance(_rit, _RIT):
            self._vision_processor = _rit.processor

        # Resolve workspace_base once — all consumers get absolute path
        resolved_ws = tg_settings.workspace_base.resolve()

        # Build channel
        self._channel = TelegramChannel(
            token=self._token,
            message_handler=self.handle_message,
            workspace_base=resolved_ws,
            tool_registry=tool_registry,
            memory=agent_loop.memory,
            onboarding_engine=self._onboarding_engine,
            image_handler=self.handle_image,
        )

        # SendFile tool
        from corpclaw_lite.extensions.tools.builtin.send_file import SendFileTool

        tool_registry.register(SendFileTool(self.send_file_callback, workspace_base=resolved_ws))

        # Health endpoint
        try:
            import importlib.util

            if importlib.util.find_spec("aiohttp") is None:
                raise ImportError("aiohttp missing")

            from corpclaw_lite.logging import health

            health_port = self._settings.logging.health_port
            self._health_runner = await health.run_health_server(port=health_port)
            logger.info("Health endpoint started on :%d/health", health_port)
        except ImportError:
            logger.info("aiohttp not installed — health endpoint disabled")

        # Hot-reloaders
        skills_dir = PROJECT_ROOT / "skills"
        plugins_dir = PROJECT_ROOT / "plugins"

        if skill_registry is not None:
            self._reloader = SkillHotReloader(skills_dir, skill_registry)
            self._reloader.start()
            logger.info("Skill hot-reloader started watching %s", skills_dir)

        if mcp_manager is not None:
            from corpclaw_lite.extensions.mcp.watcher import MCPHotReloader

            mcp_cfg_path = PROJECT_ROOT / "config" / "mcp_servers.yaml"
            self._mcp_reloader = MCPHotReloader(mcp_cfg_path, mcp_manager, tool_registry)
            self._mcp_reloader.start()
            logger.info("MCP hot-reloader started watching %s", mcp_cfg_path)

        if plugin_registry is not None and skill_registry is not None:
            self._plugin_reloader = PluginHotReloader(
                plugins_dir, plugin_registry, tool_registry, skill_registry
            )
            self._plugin_reloader.start()
            logger.info("Plugin hot-reloader started watching %s", plugins_dir)

        if stack.subagent_registry is not None:
            from corpclaw_lite.extensions.subagents.watcher import SubagentHotReloader

            subagents_dir = PROJECT_ROOT / "config" / "subagents"
            if subagents_dir.exists():
                self._subagent_reloader = SubagentHotReloader(
                    subagents_dir, stack.subagent_registry
                )
                self._subagent_reloader.start()
                logger.info("Subagent hot-reloader started watching %s", subagents_dir)

        # Start
        logger.info("Starting Telegram bot...")
        install_signal_handlers(self._shutdown_event)

        await self._channel.start()

        if tg_settings.admin_ids and self._channel.app:
            self._admin_notifier = AdminNotifier(
                bot=self._channel.app.bot,
                admin_ids=tg_settings.admin_ids,
            )
            logger.info("Admin notifier active for %d admin(s)", len(tg_settings.admin_ids))

        self._cleanup_task = asyncio.create_task(self._rate_limit_cleanup_loop())

    async def run_until_shutdown(self) -> None:
        """Block until shutdown signal."""
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Graceful shutdown in correct order."""
        if self._health_runner is not None:
            try:
                await self._health_runner.cleanup()
            except Exception as e:
                logger.warning("Health server cleanup failed: %s", e)
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        for task in self._background_tasks:
            task.cancel()
        if self._reloader is not None:
            try:
                self._reloader.stop()
            except Exception as e:
                logger.warning("SkillHotReloader stop failed: %s", e)
        if self._mcp_reloader is not None:
            try:
                self._mcp_reloader.stop()
            except Exception as e:
                logger.warning("MCPHotReloader stop failed: %s", e)
        if self._plugin_reloader is not None:
            try:
                self._plugin_reloader.stop()
            except Exception as e:
                logger.warning("PluginHotReloader stop failed: %s", e)
        if self._subagent_reloader is not None:
            try:
                self._subagent_reloader.stop()
            except Exception as e:
                logger.warning("SubagentHotReloader stop failed: %s", e)
        if self._channel is not None:
            try:
                await self._channel.stop()
            except Exception as e:
                logger.warning("Channel stop failed: %s", e)
        if self._stack is not None and self._stack.mcp_manager is not None:
            try:
                await self._stack.mcp_manager.disconnect_all()
                logger.info("MCP servers disconnected.")
            except Exception as e:
                logger.warning("MCP disconnect failed: %s", e)
        if self._stack is not None and self._stack.container_manager is not None:
            try:
                active = await self._stack.container_manager.list_active()
                for cname in active:
                    try:
                        self._stack.container_manager.stop_by_name(cname)
                    except Exception as e:
                        logger.warning("Could not stop container %s: %s", cname, e)
            except Exception as e:
                logger.warning("Container cleanup failed: %s", e)
        logger.info("Telegram bot stopped cleanly.")

    async def handle_message(self, telegram_id: str, message: str, mode: str = "execute") -> None:
        """Main message handler — replaces nested _handle_and_reply."""
        stack, channel, rate_limiter, bootstrap = self._require_started()

        tid = int(telegram_id)
        user_manager = stack.user_manager
        agent_loop = stack.loop
        container_manager = stack.container_manager

        # Access control
        if user_manager.is_session_revoked(tid):
            logger.debug("Ignoring message from revoked session telegram_id=%d", tid)
            return

        if not user_manager.is_allowed(tid):
            temp_user = User(id=0, name=f"user_{tid}", department="default", telegram_id=tid)
            await channel.send_message(temp_user, "⛔ У вас нет доступа к этому боту.")
            return

        # Get or register user
        user = await user_manager.async_get_by_telegram_id(tid)
        if not user:
            dept = user_manager.get_whitelist_department(tid)
            user = await user_manager.async_create_user(telegram_id=tid, department=dept)
            logger.info("Auto-registered user telegram_id=%d (dept=%s)", tid, dept)

        # Rate limiting (before onboarding to prevent bypass via /setup)
        allowed = await rate_limiter.check(tid)
        if not allowed:
            await channel.send_message(
                user,
                "⚠️ Слишком много сообщений. Подождите минуту и попробуйте снова.",
            )
            return

        # Onboarding intercept
        if self._onboarding_engine is not None and await self._onboarding_engine.needs_onboarding(
            tid
        ):
            if not await self._onboarding_engine.is_in_progress(tid):
                question = await self._onboarding_engine.start(tid, user.department)
                if question:
                    text = (
                        "👋 Добро пожаловать! Давай настроим меня под тебя "
                        "— это займёт пару минут.\n\n"
                        f"{question.prompt}"
                    )
                    if question.hint:
                        text += f"\n💡 {question.hint}"
                    await channel.send_message(user, text)
                    return
            else:
                next_q = await self._onboarding_engine.submit_answer(tid, message, user.department)
                if next_q:
                    text = next_q.prompt
                    if next_q.hint:
                        text += f"\n💡 {next_q.hint}"
                    await channel.send_message(user, text)
                else:
                    user = await user_manager.async_get_by_telegram_id(tid) or user
                    answers = await self._onboarding_engine.get_summary(tid)
                    name = answers.get("preferred_name", user.name)
                    await channel.send_message(
                        user,
                        f"✅ Готово, {name}! Я настроен под тебя.\n\n"
                        "Можешь задавать вопросы и давать задачи. "
                        "Для перенастройки — /setup",
                    )
                return

        # Progress indicator
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

        # Container isolation guard
        if container_manager is not None:
            try:
                await container_manager.ensure_running_async(tid)
            except ContainerManagerError as e:
                logger.error("Container failed for user %d: %s", tid, e)
                await channel.send_message(
                    user,
                    "⚠️ Изолированная рабочая среда временно недоступна.\n"
                    "Пожалуйста, повторите попытку позже или обратитесь к администратору.",
                )
                return

        # Agent execution
        run_stats = None
        try:
            base_prompt = bootstrap.get_system_prompt()
            dept_prompt = bootstrap.get_department_prompt(user.department)
            user_prompt = bootstrap.get_user_prompt(user.telegram_id) if user.telegram_id else None
            user_ctx = f"You are talking to {user.name} from the {user.department} department."
            parts_list = [p for p in [base_prompt, dept_prompt, user_prompt, user_ctx] if p]
            system_prompt: str | None = "\n\n".join(parts_list) if parts_list else None

            # Inject only relevant skill instructions into system prompt
            skill_registry = stack.skill_registry
            plugin_registry = stack.plugin_registry
            allowed_skills = (
                skill_registry.get_allowed_skills(user) if skill_registry is not None else []
            )
            plugin_skills = (
                [p.skill for p in plugin_registry.get_allowed_plugins(user) if p.skill is not None]
                if plugin_registry is not None
                else []
            )
            from corpclaw_lite.agent.prompt import build_skill_block

            all_candidate_skills = allowed_skills + plugin_skills
            # Filter skills by scope — main agent only gets skills scoped to "main"
            main_scoped = [s for s in all_candidate_skills if "*" in s.scope or "main" in s.scope]
            if stack.skill_matcher is not None:
                matched_skills = stack.skill_matcher.match(message, main_scoped)
            else:
                matched_skills = main_scoped
            skill_block = build_skill_block(matched_skills, [])
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
                few_shots=stack.few_shots,
            )
        except Exception as e:
            logger.error("AgentLoop error for user %d: %s", tid, e)
            reply = f"❌ Произошла ошибка: {e}"
            if self._admin_notifier is not None:
                error_summary = (
                    f"🔴 Agent error\n"
                    f"User: {tid} ({user.name})\n"
                    f"Error: {type(e).__name__}: {str(e)[:200]}"
                )
                _task = asyncio.create_task(self._admin_notifier.notify(error_summary))
                self._background_tasks.add(_task)
                _task.add_done_callback(self._background_tasks.discard)
        finally:
            if status_session is not None:
                await status_session.close()

        # Structured activity log
        if self._agent_activity_logger is not None:
            self._agent_activity_logger.log_request(
                user_id=str(tid),
                department=user.department,
                message_preview=message[:100],
                duration_ms=run_stats.duration_ms if run_stats is not None else 0.0,
                tools_used=run_stats.tools_used if run_stats is not None else [],
                status=run_stats.status if run_stats is not None else "error",
                error=run_stats.error if run_stats is not None else None,
            )

        await channel.send_message(user, reply)

    async def handle_image(self, telegram_id: str, image_path: Path, caption: str | None) -> None:
        """Route photo uploads directly to vision LLM, bypassing agent loop."""
        stack, channel, rate_limiter, _ = self._require_started()

        user_manager = stack.user_manager

        if not user_manager.is_allowed(int(telegram_id)):
            return
        user = await user_manager.async_get_by_telegram_id(int(telegram_id))
        if user is None:
            return

        # Session revocation + rate limiting (same checks as handle_message)
        if user_manager.is_session_revoked(int(telegram_id)):
            logger.debug("Ignoring image from revoked session telegram_id=%d", int(telegram_id))
            return
        allowed = await rate_limiter.check(int(telegram_id))
        if not allowed:
            await channel.send_message(
                user,
                "⚠️ Слишком много сообщений. Подождите минуту и попробуйте снова.",
            )
            return
        if self._vision_processor is None:
            directive = (
                f"Немедленно проанализируй изображение '{image_path.name}' "
                f"с помощью read_image и верни результат. "
                + (f"Запрос: {caption}" if caption else "")
            )
            await self.handle_message(telegram_id, directive, "execute")
            return
        prompt = caption or "Опиши это изображение подробно."

        bot = channel.bot
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id=int(telegram_id), action="typing")
        result = await self._vision_processor.describe(image_path, prompt, user)

        # Persist in agent memory — non-critical, don't block user response
        mem = stack.loop.memory
        if mem is not None:
            mem_key = user.memory_key()
            user_msg = f"[Пользователь отправил изображение: {image_path.name}] {prompt}"
            try:
                await mem.add_message(mem_key, "user", user_msg)
                await mem.add_message(mem_key, "assistant", result)
            except Exception:
                logger.error(
                    "Failed to persist image interaction in memory for user %s", telegram_id
                )

        await channel.send_message(user, result)

    async def send_file_callback(self, path: Path, user: User, caption: str) -> str:
        """File delivery callback for SendFileTool."""
        if self._channel is None:
            raise RuntimeError("TelegramChannel not initialized")
        await self._channel.send_file(user, path, caption)
        return f"File '{path.name}' sent to user."

    async def _rate_limit_cleanup_loop(self) -> None:
        """Periodic rate limiter cleanup."""
        if self._rate_limiter is None:
            raise RuntimeError("RateLimiter not initialized")
        while True:
            await asyncio.sleep(300)
            await self._rate_limiter.cleanup()
