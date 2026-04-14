from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import anyio

from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User

__all__ = [
    "SubagentDispatcher",
]

if TYPE_CHECKING:
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.llm.base import Provider
    from corpclaw_lite.security.tool_guard import ToolGuard

logger = logging.getLogger(__name__)

_SUBAGENT_TIMEOUT_MULTIPLIER = 2


class SubagentDispatcher:
    """Dispatches a subagent with its own isolated AgentLoop."""

    def __init__(
        self,
        provider: Provider,
        main_registry: ToolRegistry,
        settings: AgentSettings,
        tool_guard: ToolGuard | None = None,
        permission_checker: PermissionChecker | None = None,
    ) -> None:
        self._provider = provider
        self._main_registry = main_registry
        self._settings = settings
        self._tool_guard = tool_guard
        self._permission_checker = permission_checker

    async def dispatch(self, spec: SubagentSpec, user: User, task_context: str) -> str:
        """Run the subagent on a specific task."""
        logger.info("Dispatching subagent %s for user %s", spec.id, user.id)

        # Resolve provider: if we have a router, use subagent-specific routing
        from corpclaw_lite.llm.router import LLMRouter

        effective_provider: Provider
        if isinstance(self._provider, LLMRouter):
            effective_provider = self._provider.for_subagent(spec.id)
        else:
            effective_provider = self._provider

        # Create an isolated tool registry with ONLY the allowed tools
        isolated_registry = ToolRegistry()
        for tool_name, tool in self._main_registry.items().items():
            if "*" in spec.allowed_tools or tool_name in spec.allowed_tools:
                isolated_registry.register(tool)

        # Load system prompt: calibrated override > prompt_path > description fallback
        system_prompt = f"You are a specialized subagent: {spec.name}.\n{spec.description}\n"
        if spec.prompt_path:
            from pathlib import Path

            from corpclaw_lite.paths import PROJECT_ROOT

            prompt_file = Path(spec.prompt_path)

            # Check for calibrated override first
            calibrated_prompt = (
                PROJECT_ROOT
                / "config"
                / "calibrated"
                / "bootstrap"
                / "subagents"
                / prompt_file.name
            )
            aio_calibrated = anyio.Path(calibrated_prompt)
            if await aio_calibrated.exists() and await aio_calibrated.is_file():
                system_prompt = await aio_calibrated.read_text(encoding="utf-8")
                logger.debug(
                    "Subagent %s: using calibrated prompt from %s",
                    spec.id,
                    calibrated_prompt,
                )
            else:
                aio_prompt = anyio.Path(prompt_file)
                if await aio_prompt.exists() and await aio_prompt.is_file():
                    system_prompt = await aio_prompt.read_text(encoding="utf-8")
                    logger.debug("Loaded prompt for subagent %s from %s", spec.id, prompt_file)
                else:
                    logger.warning(
                        "Subagent %s prompt_path '%s' not found, using description fallback",
                        spec.id,
                        spec.prompt_path,
                    )

        # Setup isolated loop — pass security guards through from parent
        loop = AgentLoop(
            AgentConfig(
                provider=effective_provider,
                registry=isolated_registry,
                settings=self._settings,
                tool_guard=self._tool_guard,
                permission_checker=self._permission_checker,
            )
        )

        timeout_seconds = self._settings.max_wall_time_ms / 1000 * _SUBAGENT_TIMEOUT_MULTIPLIER

        try:
            result, _ = await asyncio.wait_for(
                loop.run(user, task_context, system_prompt=system_prompt),
                timeout=timeout_seconds,
            )
            return result
        except TimeoutError:
            logger.error("Subagent %s timed out after %.0fs", spec.id, timeout_seconds)
            return f"Subagent error: execution timed out after {int(timeout_seconds)}s"
        except Exception as e:
            logger.error("Subagent %s failed: %s", spec.id, e)
            return f"Subagent error: {e}"
