"""TelegramBotOrchestrator — orchestrates the full Telegram bot lifecycle.

Extracts the monolithic ``run_telegram_bot()`` closure into a class with
clear separation between startup, message handling, and shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
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
from corpclaw_lite.logging.trace import log_event
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
        self._queue_notify_task: asyncio.Task[None] | None = None
        self._shutdown_event = asyncio.Event()
        self._agent_activity_logger: AgentLogger | None = None
        self._active_user_requests: set[int] = set()
        self._active_user_requests_lock = asyncio.Lock()
        self._started = False

    async def _try_start_user_request(self, telegram_id: int) -> bool:
        """Return False when the user already has an active workflow."""
        async with self._active_user_requests_lock:
            if telegram_id in self._active_user_requests:
                return False
            self._active_user_requests.add(telegram_id)
            return True

    async def _finish_user_request(self, telegram_id: int) -> None:
        """Mark a user's active workflow as finished."""
        async with self._active_user_requests_lock:
            self._active_user_requests.discard(telegram_id)

    async def check_channel_access(self, telegram_id: int, action: str) -> bool:
        """Shared Telegram preflight for commands, callbacks and uploads."""
        stack, channel, rate_limiter, _ = self._require_started()
        user_manager = stack.user_manager

        if user_manager.is_session_revoked(telegram_id):
            logger.debug(
                "Ignoring Telegram action from revoked session telegram_id=%d action=%s",
                telegram_id,
                action,
            )
            return False

        temp_user = User(
            id=0,
            name=f"user_{telegram_id}",
            department="default",
            telegram_id=telegram_id,
        )
        if not user_manager.is_allowed(telegram_id):
            await channel.send_message(temp_user, "⛔ У вас нет доступа к этому боту.")
            return False

        user = await user_manager.async_get_by_telegram_id(telegram_id)
        if not user:
            dept = user_manager.get_whitelist_department(telegram_id)
            user = await user_manager.async_create_user(telegram_id=telegram_id, department=dept)
            logger.info("Auto-registered user telegram_id=%d (dept=%s)", telegram_id, dept)

        allowed = await rate_limiter.check(telegram_id)
        if not allowed:
            await channel.send_message(
                user,
                "⚠️ Слишком много сообщений. Подождите минуту и попробуйте снова.",
            )
            return False
        return True

    async def _resolve_user_by_telegram_id(self, telegram_id: int) -> User | None:
        """Return the canonical user row for a Telegram identity."""
        stack, _, _, _ = self._require_started()
        return await stack.user_manager.async_get_by_telegram_id(telegram_id)

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
            trace_enabled=log_cfg.trace_enabled,
            trace_level=log_cfg.trace_level,
            trace_preview_chars=log_cfg.trace_preview_chars,
        )
        self._agent_activity_logger = AgentLogger(log_dir=PROJECT_ROOT / log_cfg.log_dir)

        stack = build_agent_stack(self._settings)
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
            cache_reset_callback=self._mark_user_cache_reset,
            setup_handler=self.handle_setup,
            access_checker=self.check_channel_access,
            user_resolver=self._resolve_user_by_telegram_id,
            tg_settings=tg_settings,
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

        # Hot-reloaders — watch default + overlay dirs from extensions.extra_paths.
        from corpclaw_lite.extensions.paths import resolve_dirs as _resolve_dirs

        skills_dirs: list[str | Path] = list(_resolve_dirs("skills", self._settings, PROJECT_ROOT))
        plugins_dirs: list[str | Path] = list(
            _resolve_dirs("plugins", self._settings, PROJECT_ROOT)
        )

        if skill_registry is not None:
            self._reloader = SkillHotReloader(skills_dirs, skill_registry)
            self._reloader.start()
            logger.info("Skill hot-reloader started watching %s", skills_dirs)

        if mcp_manager is not None:
            from corpclaw_lite.extensions.mcp.watcher import MCPHotReloader

            mcp_cfg_paths: list[str | Path] = list(
                _resolve_dirs("mcp", self._settings, PROJECT_ROOT)
            )
            self._mcp_reloader = MCPHotReloader(mcp_cfg_paths, mcp_manager, tool_registry)
            self._mcp_reloader.start()
            logger.info("MCP hot-reloader started watching %s", mcp_cfg_paths)

        if plugin_registry is not None and skill_registry is not None:
            self._plugin_reloader = PluginHotReloader(
                plugins_dirs, plugin_registry, tool_registry, skill_registry
            )
            self._plugin_reloader.start()
            logger.info("Plugin hot-reloader started watching %s", plugins_dirs)

        if stack.subagent_registry is not None:
            from corpclaw_lite.extensions.subagents.watcher import SubagentHotReloader

            subagents_dirs: list[str | Path] = [
                d for d in _resolve_dirs("subagents", self._settings, PROJECT_ROOT) if d.exists()
            ]
            if subagents_dirs:
                self._subagent_reloader = SubagentHotReloader(
                    subagents_dirs, stack.subagent_registry
                )
                self._subagent_reloader.start()
                logger.info("Subagent hot-reloader started watching %s", subagents_dirs)

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

        # Queue notification loop
        from corpclaw_lite.llm.router import LLMRouter

        if (
            isinstance(agent_loop.provider, LLMRouter)
            and agent_loop.provider.has_queue
            and self._settings.llm.queue.notify_position
        ):
            self._queue_notify_task = asyncio.create_task(
                self._queue_notification_loop(
                    agent_loop.provider.queue,
                    self._settings.llm.queue.notify_interval_seconds,
                )
            )
            logger.info(
                "Queue notification loop started (interval=%ds)",
                self._settings.llm.queue.notify_interval_seconds,
            )
        self._started = True

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
        if self._queue_notify_task is not None:
            self._queue_notify_task.cancel()
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
        if self._started:
            logger.info("Telegram bot stopped cleanly.")
            self._started = False
        else:
            logger.debug("Telegram bot cleanup completed before full startup.")

    async def handle_setup(self, telegram_id: int) -> str:
        """Reset and restart onboarding under the shared per-user workflow lock."""
        stack, _, _, _ = self._require_started()
        if self._onboarding_engine is None:
            return "⚠️ Настройка недоступна."

        user_manager = stack.user_manager
        user = await user_manager.async_get_by_telegram_id(telegram_id)
        if user is None:
            dept = user_manager.get_whitelist_department(telegram_id)
            user = await user_manager.async_create_user(
                telegram_id=telegram_id,
                department=dept,
            )

        if not await self._try_start_user_request(user.id):
            return (
                "⏳ Предыдущая задача ещё выполняется. "
                "Дождитесь ответа и отправьте новый запрос после завершения."
            )

        try:
            await self._onboarding_engine.reset(user.id)
            question = await self._onboarding_engine.start(user.id, user.department)
            if question is None:
                return "⚠️ Не удалось начать настройку. Попробуйте позже."

            text = f"🔄 Перенастройка! Предыдущие настройки будут обновлены.\n\n{question.prompt}"
            if question.hint:
                text += f"\n💡 {question.hint}"
            logger.info("User %d started /setup", telegram_id)
            return text
        finally:
            await self._finish_user_request(user.id)

    async def handle_message(
        self,
        telegram_id: str,
        message: str,
        mode: str = "execute",
        *,
        prechecked_access: bool = False,
    ) -> None:
        """Main message handler — replaces nested _handle_and_reply."""
        stack, channel, rate_limiter, bootstrap = self._require_started()

        tid = int(telegram_id)
        user_manager = stack.user_manager
        agent_loop = stack.loop
        container_manager = stack.container_manager

        # Access control
        if not prechecked_access:
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
        if not prechecked_access:
            allowed = await rate_limiter.check(tid)
            if not allowed:
                await channel.send_message(
                    user,
                    "⚠️ Слишком много сообщений. Подождите минуту и попробуйте снова.",
                )
                return

        # Queue position notification
        from corpclaw_lite.llm.router import LLMRouter

        if (
            isinstance(agent_loop.provider, LLMRouter)
            and agent_loop.provider.has_queue
            and self._settings.llm.queue.notify_position
        ):
            queue = agent_loop.provider.queue
            if queue is not None:
                position = queue.get_position(user.memory_key())
                if position is not None:
                    est_wait = queue.get_estimated_wait(user.memory_key())
                    wait_text = f" ~{int(est_wait)}с." if est_wait is not None else "."
                    await channel.send_message(
                        user,
                        f"Запрос принят. Вы #{position + 1} в очереди.{wait_text}",
                    )

        # Onboarding intercept
        if self._onboarding_engine is not None and await self._onboarding_engine.needs_onboarding(
            user.id
        ):
            if not await self._try_start_user_request(user.id):
                await channel.send_message(
                    user,
                    "⏳ Предыдущая задача ещё выполняется. "
                    "Дождитесь ответа и отправьте новый запрос после завершения.",
                )
                return
            try:
                if not await self._onboarding_engine.is_in_progress(user.id):
                    question = await self._onboarding_engine.start(user.id, user.department)
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
                    next_q = await self._onboarding_engine.submit_answer(
                        user.id, message, user.department
                    )
                    if next_q:
                        text = next_q.prompt
                        if next_q.hint:
                            text += f"\n💡 {next_q.hint}"
                        await channel.send_message(user, text)
                    else:
                        user = await user_manager.async_get_by_telegram_id(tid) or user
                        answers = await self._onboarding_engine.get_summary(user.id)
                        name = answers.get("preferred_name", user.name)
                        await channel.send_message(
                            user,
                            f"✅ Готово, {name}! Я настроен под тебя.\n\n"
                            "Можешь задавать вопросы и давать задачи. "
                            "Для перенастройки — /setup",
                        )
                    return
            finally:
                await self._finish_user_request(user.id)

        if not await self._try_start_user_request(user.id):
            await channel.send_message(
                user,
                "⏳ Предыдущая задача ещё выполняется. "
                "Дождитесь ответа и отправьте новый запрос после завершения.",
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
                await container_manager.ensure_running_async(user.id)
            except ContainerManagerError as e:
                logger.error("Container failed for user %d: %s", user.id, e)
                await channel.send_message(
                    user,
                    "⚠️ Изолированная рабочая среда временно недоступна.\n"
                    "Пожалуйста, повторите попытку позже или обратитесь к администратору.",
                )
                await self._finish_user_request(user.id)
                return

        # Agent execution
        run_stats = None
        try:
            base_prompt = bootstrap.get_system_prompt()
            dept_prompt = bootstrap.get_department_prompt(user.department)
            user_prompt = bootstrap.get_user_prompt(user.id, user.telegram_id)
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
                on_llm_stage=(
                    status_session.mark_llm_stage if status_session is not None else None
                ),
                tools_enabled=(mode == "execute"),
                few_shots=stack.few_shots,
                channel="telegram",
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
                user_id=user.memory_key(),
                department=user.department,
                message_preview=message[:100],
                duration_ms=run_stats.duration_ms if run_stats is not None else 0.0,
                tools_used=run_stats.tools_used if run_stats is not None else [],
                status=run_stats.status if run_stats is not None else "error",
                error=run_stats.error if run_stats is not None else None,
                run_id=run_stats.run_id if run_stats is not None else None,
                channel="telegram",
                iterations=run_stats.iterations if run_stats is not None else None,
                llm_calls=run_stats.llm_calls if run_stats is not None else None,
                input_tokens=run_stats.input_tokens if run_stats is not None else None,
                output_tokens=run_stats.output_tokens if run_stats is not None else None,
                total_tokens=run_stats.total_tokens if run_stats is not None else None,
                latest_total_tokens=(
                    run_stats.latest_total_tokens if run_stats is not None else None
                ),
                stream_stats=(
                    {
                        "calls": run_stats.llm_stream_calls,
                        "fallbacks": run_stats.llm_stream_fallbacks,
                        "stalls": run_stats.llm_stream_stalls,
                        "events": run_stats.llm_stream_events,
                        "first_event_ms": run_stats.llm_first_event_ms,
                        "first_content_ms": run_stats.llm_first_content_ms,
                        "first_tool_call_ms": run_stats.llm_first_tool_call_ms,
                    }
                    if run_stats is not None
                    else None
                ),
            )

        try:
            await channel.send_message(user, reply)
        finally:
            await self._finish_user_request(user.id)

    async def handle_image(
        self,
        telegram_id: str,
        image_path: Path,
        caption: str | None,
        *,
        prechecked_access: bool = False,
    ) -> None:
        """Route photo uploads directly to vision LLM, bypassing agent loop."""
        stack, channel, rate_limiter, _ = self._require_started()

        user_manager = stack.user_manager
        tid = int(telegram_id)

        if not prechecked_access:
            if user_manager.is_session_revoked(tid):
                logger.debug("Ignoring image from revoked session telegram_id=%d", tid)
                return
            if not user_manager.is_allowed(tid):
                return

        user = await user_manager.async_get_by_telegram_id(tid)
        if user is None:
            if not user_manager.is_allowed(tid):
                return
            dept = user_manager.get_whitelist_department(tid)
            user = await user_manager.async_create_user(telegram_id=tid, department=dept)

        # Session revocation + rate limiting (same checks as handle_message)
        if not prechecked_access:
            allowed = await rate_limiter.check(tid)
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
            await self.handle_message(
                telegram_id,
                directive,
                "execute",
                prechecked_access=prechecked_access,
            )
            return
        if not await self._try_start_user_request(user.id):
            await channel.send_message(
                user,
                "⏳ Предыдущая задача ещё выполняется. "
                "Дождитесь ответа и отправьте новый запрос после завершения.",
            )
            return
        prompt = caption or "Опиши это изображение подробно."
        run_id = uuid.uuid4().hex
        t0 = time.monotonic()
        log_event(
            "image_request_started",
            run_id,
            user_id=user.id,
            telegram_id=telegram_id,
            channel="telegram",
            image_name=image_path.name,
            prompt_len=len(prompt),
        )

        bot = channel.bot
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id=int(telegram_id), action="typing")
        status = "ok"
        error: str | None = None
        try:
            result = await self._vision_processor.describe(image_path, prompt, user)
        except Exception as e:
            status = "error"
            error = f"{type(e).__name__}: {e}"
            log_event(
                "image_request_finished",
                run_id,
                status=status,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
                error=error,
            )
            await self._finish_user_request(user.id)
            raise

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

        try:
            await channel.send_message(user, result)
        except Exception:
            await self._finish_user_request(user.id)
            raise
        duration_ms = (time.monotonic() - t0) * 1000
        log_event(
            "image_request_finished",
            run_id,
            status=status,
            duration_ms=round(duration_ms, 1),
            result_len=len(result),
        )
        if self._agent_activity_logger is not None:
            self._agent_activity_logger.log_request(
                user_id=user.memory_key(),
                department=user.department,
                message_preview=f"[image] {image_path.name}",
                duration_ms=duration_ms,
                tools_used=["read_image"],
                status=status,
                error=error,
                run_id=run_id,
                channel="telegram",
            )
        await self._finish_user_request(user.id)

    async def send_file_callback(self, path: Path, user: User, caption: str) -> str:
        """File delivery callback for SendFileTool."""
        if self._channel is None:
            raise RuntimeError("TelegramChannel not initialized")
        await self._channel.send_file(user, path, caption)
        return f"File '{path.name}' sent to user."

    async def _mark_user_cache_reset(self, telegram_id: str) -> None:
        """Invalidate LLM cache state for a user after /new clears memory."""
        if self._stack is None:
            return
        from corpclaw_lite.llm.router import LLMRouter

        provider = self._stack.loop.provider
        if isinstance(provider, LLMRouter):
            await provider.mark_user_cache_reset(telegram_id)

    async def _rate_limit_cleanup_loop(self) -> None:
        """Periodic rate limiter cleanup."""
        if self._rate_limiter is None:
            raise RuntimeError("RateLimiter not initialized")
        while True:
            await asyncio.sleep(300)
            await self._rate_limiter.cleanup()

    async def _queue_notification_loop(
        self,
        queue: object,
        interval_seconds: int,
    ) -> None:
        """Periodically notify waiting users about their queue position."""
        import time

        from corpclaw_lite.llm.queue import LLMRequestQueue

        assert isinstance(queue, LLMRequestQueue)
        channel = self._channel
        user_manager = self._stack.user_manager if self._stack else None
        if channel is None or user_manager is None:
            return

        while True:
            await asyncio.sleep(interval_seconds)
            waiting = queue.get_waiting_entries()
            if not waiting:
                continue
            now = time.monotonic()
            for entry in waiting:
                if now - entry.last_notified_at < interval_seconds:
                    continue
                position = queue.get_position(entry.user_id)
                if position is None:
                    continue
                est = queue.get_estimated_wait(entry.user_id)
                wait_text = f" ~{int(est)}с." if est is not None else "."
                try:
                    tid = int(entry.user_id)
                    user = await user_manager.async_get_by_telegram_id(tid)
                    if user is not None:
                        await channel.send_message(
                            user,
                            f"Обновление: Вы #{position + 1} в очереди.{wait_text}",
                        )
                        entry.last_notified_at = time.monotonic()
                except Exception as e:
                    logger.warning("Queue notification failed for user %s: %s", entry.user_id, e)
