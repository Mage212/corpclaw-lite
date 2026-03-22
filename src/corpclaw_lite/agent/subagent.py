from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User

if TYPE_CHECKING:
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.llm.base import Provider
    from corpclaw_lite.security.tool_guard import ToolGuard

logger = logging.getLogger(__name__)


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

        # Create an isolated tool registry with ONLY the allowed tools
        isolated_registry = ToolRegistry()
        for tool_name, tool in self._main_registry._tools.items():  # type: ignore[attr-defined]
            if "*" in spec.allowed_tools or tool_name in spec.allowed_tools:
                isolated_registry.register(tool)

        # Load system prompt from prompt_path if provided, else fall back to description
        system_prompt = f"You are a specialized subagent: {spec.name}.\n{spec.description}\n"
        if spec.prompt_path:
            from pathlib import Path

            prompt_file = Path(spec.prompt_path)
            if prompt_file.exists() and prompt_file.is_file():
                system_prompt = prompt_file.read_text(encoding="utf-8")
                logger.debug("Loaded prompt for subagent %s from %s", spec.id, prompt_file)
            else:
                logger.warning(
                    "Subagent %s prompt_path '%s' not found, using description fallback",
                    spec.id,
                    spec.prompt_path,
                )

        # Setup isolated loop — pass security guards through from parent
        loop = AgentLoop(
            provider=self._provider,
            registry=isolated_registry,
            settings=self._settings,
            tool_guard=self._tool_guard,
            permission_checker=self._permission_checker,
        )

        try:
            # Pass system_prompt as dedicated system message, task as user message
            result = await loop.run(user, task_context, system_prompt=system_prompt)
            return result
        except Exception as e:
            logger.error("Subagent %s failed: %s", spec.id, e)
            return f"Subagent error: {e}"
