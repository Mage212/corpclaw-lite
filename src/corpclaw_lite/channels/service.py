from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from corpclaw_lite.agent.loop import RunStats
from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.container.manager import ContainerManagerError
from corpclaw_lite.exceptions import LLMBackendUnavailableError
from corpclaw_lite.extensions.tools.builtin._path_utils import user_workspace_path
from corpclaw_lite.llm.queue import LLMQueueStatus
from corpclaw_lite.logging.agent_logger import AgentLogger
from corpclaw_lite.paths import PROJECT_ROOT
from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)

__all__ = [
    "AgentRequestCallbacks",
    "AgentRequestResult",
    "AgentRequestService",
    "RunningRequest",
    "is_llm_transport_error",
]


@dataclass(frozen=True, slots=True)
class RunningRequest:
    """In-flight workflow metadata for a single user (B-090 / DC-011).

    ``session_id`` is set only for long agent runs (badge + reject title).
    Short mutations (create/activate/delete/reset/compress) hold the mutex with
    ``session_id=None`` so the chat-list badge does not flicker.
    """

    session_id: int | None = None
    title: str | None = None


_LLM_TRANSPORT_ERROR_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "NetworkError",
    "PoolTimeout",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "TimeoutException",
    "WriteError",
    "WriteTimeout",
}
_LLM_TRANSPORT_MODULE_PREFIXES = ("openai", "httpx", "httpcore", "anyio")
_LLM_UPSTREAM_UNAVAILABLE_MARKERS = (
    "connection refused",
    "upstream_error",
    "bad gateway",
    "error code: 502",
    "service unavailable",
    "error code: 503",
)


def is_llm_transport_error(exc: BaseException) -> bool:
    """Return True for expected network/transport failures from LLM clients."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        cls = type(current)
        module = cls.__module__
        name = cls.__name__
        text = str(current).lower()
        is_transport_module = module.startswith(_LLM_TRANSPORT_MODULE_PREFIXES)
        if is_transport_module and (
            name in _LLM_TRANSPORT_ERROR_NAMES
            or "connection error" in text
            or "all connection attempts failed" in text
            or any(marker in text for marker in _LLM_UPSTREAM_UNAVAILABLE_MARKERS)
        ):
            return True
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return False


@dataclass(slots=True)
class AgentRequestCallbacks:
    """Channel callbacks used during an agent request."""

    request_approval: Callable[[str, str], Awaitable[bool]] | None = None
    on_tool_start: Callable[[str], None] | None = None
    on_tool_batch_start: Callable[[list[str]], None] | None = None
    on_llm_stage: Callable[[str], None] | None = None
    on_llm_queue_status: Callable[[LLMQueueStatus], None] | None = None
    on_subagent_tool_start: Callable[[str, str], None] | None = None
    on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None = None
    on_subagent_llm_stage: Callable[[str, str], None] | None = None
    on_subagent_llm_queue_status: Callable[[str, LLMQueueStatus], None] | None = None


@dataclass(slots=True)
class AgentRequestResult:
    """Result returned by a channel-neutral agent request."""

    reply: str
    stats: RunStats


class AgentRequestService:
    """Shared channel-neutral request orchestration for AgentLoop."""

    def __init__(
        self,
        *,
        stack: object,
        bootstrap: BootstrapLoader | None = None,
        workspace_base: Path | None = None,
        activity_logger: AgentLogger | None = None,
        llm_provider_name: str | None = None,
        llm_base_url: str | None = None,
    ) -> None:
        from corpclaw_lite.agent.factory import AgentStack

        if not isinstance(stack, AgentStack):
            raise TypeError("stack must be AgentStack")
        self._stack = stack
        # bootstrap retained for constructor back-compat; prompt assembly is on the loop (B-111).
        _ = bootstrap or BootstrapLoader(PROJECT_ROOT / "config" / "bootstrap")
        self._workspace_base = (workspace_base or PROJECT_ROOT / "workspaces").resolve()
        self._activity_logger = activity_logger
        self._llm_provider_name = llm_provider_name
        self._llm_base_url = llm_base_url
        self._active_user_requests: dict[int, RunningRequest] = {}
        self._active_user_requests_lock = asyncio.Lock()

    def get_user_workspace(self, user: User) -> Path:
        """Return the host workspace for a user, creating it if needed."""
        workspace = user_workspace_path(self._workspace_base, user)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    async def try_start_user_request(
        self,
        user_id: int,
        *,
        session_id: int | None = None,
        title: str | None = None,
    ) -> bool:
        """Return False when the user already has an active workflow.

        Pass ``session_id`` (and optional ``title``) only for long agent runs so
        B-090 can expose is_running + reject reason. Short mutations omit them.
        """
        async with self._active_user_requests_lock:
            if user_id in self._active_user_requests:
                return False
            self._active_user_requests[user_id] = RunningRequest(
                session_id=session_id,
                title=title,
            )
            return True

    async def finish_user_request(self, user_id: int) -> RunningRequest | None:
        """Mark a user's active workflow as finished; return what was held (if any)."""
        async with self._active_user_requests_lock:
            return self._active_user_requests.pop(user_id, None)

    async def get_running_request(self, user_id: int) -> RunningRequest | None:
        """Return in-flight metadata for *user_id*, or None if idle."""
        async with self._active_user_requests_lock:
            return self._active_user_requests.get(user_id)

    async def active_user_count(self) -> int:
        """Number of users with an in-flight workflow (not LLM slot count)."""
        async with self._active_user_requests_lock:
            return len(self._active_user_requests)

    async def reset_user_context(self, user: User) -> None:
        """Invalidate LLM KV-cache after a session reset (B-106).

        Transcript lives in ``ChatContextStore`` / ``WebChatStore`` — callers
        archive the chat session separately (CASCADE clears context rows).
        ``SQLiteMemory`` is facts-only and is intentionally not cleared here
        (facts are cross-chat personalization).
        """
        from corpclaw_lite.llm.router import LLMRouter

        provider = self._stack.loop.provider
        if isinstance(provider, LLMRouter):
            await provider.mark_user_cache_reset(user.memory_key())

    async def restore_user_context(self, user: User, session_id: int) -> bool:
        """Validate session ownership and that context-store has transcript (B-104).

        The store is the sole LLM transcript source — ``AgentLoop.run`` loads full
        tool_calls/tool-role via ``list_context``. This method only:
        1. IDOR-checks ownership (B-067)
        2. Confirms the store has messages for ``session_id``
        3. Invalidates slot KV-cache for the user

        Returns True if context is available; False if empty / missing / unauthorized
        (caller falls back to ``reset_user_context``).
        """
        store = self._stack.chat_context_store
        if store is None:
            return False
        chat_store = self._stack.chat_store
        if chat_store is not None:
            session = await chat_store.get_session(user.memory_key(), session_id)
            if session is None:
                logger.warning(
                    "[session=%s] restore_user_context: not owned by user %s (IDOR blocked)",
                    session_id,
                    user.id,
                )
                return False
        try:
            messages = await store.list_context(session_id)
        except Exception:
            logger.warning(
                "[session=%s] restore_user_context: context-store load failed",
                session_id,
                exc_info=True,
            )
            return False
        if not messages:
            return False
        from corpclaw_lite.llm.router import LLMRouter

        provider = self._stack.loop.provider
        if isinstance(provider, LLMRouter):
            await provider.mark_user_cache_reset(user.memory_key())
        return True

    async def compress_user_context(
        self, user: User, session_id: int | None = None
    ) -> tuple[bool, str]:
        """On-demand compression of a chat's full LLM context (B-105).

        Thin wrapper over ``AgentLoop.compress_now``; requires ``session_id`` and
        a configured ChatContextStore. The caller (orchestrator) holds the
        single-in-flight lock so this never races an active run.
        Returns ``(ok, message)``.
        """
        # B-067: verify ownership at the service layer (not just in the
        # orchestrator) so the public method cannot be used to compress — and
        # thereby re-attribute via replace_context — another user's chat.
        if session_id is not None:
            chat_store = self._stack.chat_store
            if chat_store is not None:
                session = await chat_store.get_session(user.memory_key(), session_id)
                if session is None:
                    logger.warning(
                        "[session=%s] compress_user_context: session not owned by"
                        " user %s (IDOR blocked)",
                        session_id,
                        user.id,
                    )
                    return False, "Чат не найден или нет доступа."
        return await self._stack.loop.compress_now(user, session_id=session_id)

    async def build_system_prompt(self, user: User) -> str | None:
        """Static system prompt for web preview (B-111).

        Delegates to ``AgentLoop.assemble_system_prompt`` so preview matches the
        layers ``run()`` uses (base + dept + onboarding + instructions + tone).
        Per-turn facts/recent-files are not included (they need an active run).
        """
        return await self._stack.loop.assemble_system_prompt(user)

    async def run(
        self,
        *,
        user: User,
        message: str,
        mode: str = "execute",
        channel: str,
        callbacks: AgentRequestCallbacks | None = None,
        depth_mode: str | None = None,
        session_id: int | None = None,
    ) -> AgentRequestResult:
        """Run an agent request with shared skill matching, container and logging.

        B-111: user-context prompt layers are assembled inside ``AgentLoop.run``;
        this service only matches skills and passes the skill block as extras.
        """
        callbacks = callbacks or AgentRequestCallbacks()
        stack = self._stack
        agent_loop = stack.loop
        container_manager = stack.container_manager
        run_stats: RunStats | None = None

        if container_manager is not None:
            try:
                await container_manager.ensure_running_async(user.id)
            except ContainerManagerError:
                logger.exception("Container failed for user %s", user.memory_key())
                raise

        skill_registry = stack.skill_registry
        plugin_registry = stack.plugin_registry
        allowed_skills = skill_registry.get_allowed_skills(user) if skill_registry else []
        plugin_skills = (
            [p.skill for p in plugin_registry.get_allowed_plugins(user) if p.skill is not None]
            if plugin_registry is not None
            else []
        )
        all_candidate_skills = allowed_skills + plugin_skills
        main_scoped = [s for s in all_candidate_skills if "*" in s.scope or "main" in s.scope]
        matched_skills = (
            stack.skill_matcher.match(message, main_scoped)
            if stack.skill_matcher is not None
            else main_scoped
        )

        from corpclaw_lite.agent.prompt import build_skill_block

        skill_block = build_skill_block(matched_skills, [])
        # B-111: only skills as system_prompt extras — loop owns user-context.
        system_prompt = skill_block if skill_block else None

        try:
            reply, run_stats = await agent_loop.run(
                user,
                message,
                system_prompt=system_prompt,
                approval_callback=callbacks.request_approval,
                on_tool_start=callbacks.on_tool_start,
                on_tool_batch_start=callbacks.on_tool_batch_start,
                on_llm_stage=callbacks.on_llm_stage,
                on_llm_queue_status=callbacks.on_llm_queue_status,
                on_subagent_tool_start=callbacks.on_subagent_tool_start,
                on_subagent_tool_batch_start=callbacks.on_subagent_tool_batch_start,
                on_subagent_llm_stage=callbacks.on_subagent_llm_stage,
                on_subagent_llm_queue_status=callbacks.on_subagent_llm_queue_status,
                tools_enabled=(mode == "execute"),
                few_shots=stack.few_shots,
                channel=channel,
                depth_mode=depth_mode,  # type: ignore[arg-type]
                session_id=session_id,
            )
        except Exception as e:
            if is_llm_transport_error(e):
                from corpclaw_lite.logging import health

                health.increment("llm_backend_unavailable")
                logger.warning(
                    "LLM backend unavailable: channel=%s user_id=%s provider=%s base_url=%s "
                    "error=%s",
                    channel,
                    user.memory_key(),
                    self._llm_provider_name or "(unknown)",
                    self._llm_base_url or "(unknown)",
                    e,
                )
                raise LLMBackendUnavailableError(
                    provider_name=self._llm_provider_name,
                    base_url=self._llm_base_url,
                    cause=e,
                ) from e
            raise
        finally:
            if run_stats is not None and self._activity_logger is not None:
                self._activity_logger.log_request(
                    user_id=user.memory_key(),
                    department=user.department,
                    message_preview=message[:100],
                    duration_ms=run_stats.duration_ms,
                    tools_used=run_stats.tools_used,
                    status=run_stats.status,
                    error=run_stats.error,
                    run_id=run_stats.run_id,
                    channel=channel,
                    iterations=run_stats.iterations,
                    llm_calls=run_stats.llm_calls,
                    input_tokens=run_stats.input_tokens,
                    output_tokens=run_stats.output_tokens,
                    total_tokens=run_stats.total_tokens,
                    latest_total_tokens=run_stats.latest_total_tokens,
                    stream_stats={
                        "calls": run_stats.llm_stream_calls,
                        "fallbacks": run_stats.llm_stream_fallbacks,
                        "stalls": run_stats.llm_stream_stalls,
                        "events": run_stats.llm_stream_events,
                        "first_event_ms": run_stats.llm_first_event_ms,
                        "first_content_ms": run_stats.llm_first_content_ms,
                        "first_tool_call_ms": run_stats.llm_first_tool_call_ms,
                    },
                )

        assert run_stats is not None
        return AgentRequestResult(reply=reply, stats=run_stats)
