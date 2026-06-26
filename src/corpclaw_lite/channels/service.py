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
    "is_llm_transport_error",
]

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
        self._bootstrap = bootstrap or BootstrapLoader(PROJECT_ROOT / "config" / "bootstrap")
        self._workspace_base = (workspace_base or PROJECT_ROOT / "workspaces").resolve()
        self._activity_logger = activity_logger
        self._llm_provider_name = llm_provider_name
        self._llm_base_url = llm_base_url
        self._active_user_requests: set[int] = set()
        self._active_user_requests_lock = asyncio.Lock()

    def get_user_workspace(self, user: User) -> Path:
        """Return the host workspace for a user, creating it if needed."""
        workspace = user_workspace_path(self._workspace_base, user)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    async def try_start_user_request(self, user_id: int) -> bool:
        """Return False when the user already has an active workflow."""
        async with self._active_user_requests_lock:
            if user_id in self._active_user_requests:
                return False
            self._active_user_requests.add(user_id)
            return True

    async def finish_user_request(self, user_id: int) -> None:
        """Mark a user's active workflow as finished."""
        async with self._active_user_requests_lock:
            self._active_user_requests.discard(user_id)

    async def reset_user_context(self, user: User) -> None:
        """Clear conversation memory and invalidate LLM cache state for a user."""
        memory = self._stack.loop.memory
        if memory is not None:
            await memory.clear(user.memory_key())

        from corpclaw_lite.llm.router import LLMRouter

        provider = self._stack.loop.provider
        if isinstance(provider, LLMRouter):
            await provider.mark_user_cache_reset(user.memory_key())

    async def run(
        self,
        *,
        user: User,
        message: str,
        mode: str = "execute",
        channel: str,
        callbacks: AgentRequestCallbacks | None = None,
        depth_mode: str | None = None,
    ) -> AgentRequestResult:
        """Run an agent request with shared prompt, skill, container and logging setup."""
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

        base_prompt = self._bootstrap.get_system_prompt()
        dept_prompt = self._bootstrap.get_department_prompt(user.department)
        user_prompt = self._bootstrap.get_user_prompt(user.id, user.telegram_id)
        user_ctx = f"You are talking to {user.name} from the {user.department} department."
        parts = [p for p in [base_prompt, dept_prompt, user_prompt, user_ctx] if p]
        system_prompt: str | None = "\n\n".join(parts) if parts else None

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
        if skill_block:
            system_prompt = (system_prompt or "") + skill_block

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
