import logging
from typing import TYPE_CHECKING

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User

if TYPE_CHECKING:
    from corpclaw_lite.llm.base import Provider

logger = logging.getLogger(__name__)


class SubagentDispatcher:
    """Dispatches a subagent with its own isolated AgentLoop."""

    def __init__(
        self,
        provider: "Provider",
        main_registry: ToolRegistry,
        settings: AgentSettings,
    ) -> None:
        self._provider = provider
        self._main_registry = main_registry
        self._settings = settings

    async def dispatch(self, spec: SubagentSpec, user: User, task_context: str) -> str:
        """Run the subagent on a specific task."""
        logger.info(f"Dispatching subagent {spec.id} for user {user.id}")

        # Create an isolated tool registry with ONLY the allowed tools
        isolated_registry = ToolRegistry()
        for tool_name, tool in self._main_registry._tools.items():  # type: ignore
            if "*" in spec.allowed_tools or tool_name in spec.allowed_tools:
                isolated_registry.register(tool)

        # Read the prompt for this subagent if available
        system_prompt = f"You are a specialized subagent: {spec.name}.\n{spec.description}\n"
        if spec.prompt_path:
            # We assume prompt_path is absolute or handled elsewhere, for simplicity
            pass  # TODO: load from prompt_path

        # Setup isolated loop
        loop = AgentLoop(
            provider=self._provider,
            registry=isolated_registry,
            settings=self._settings,
        )

        try:
            # Run the isolated ReAct loop
            # Provide the task context as the "message"
            full_message = f"{system_prompt}\n\nTask:\n{task_context}"
            result = await loop.run(user, full_message)
            return result
        except Exception as e:
            logger.error(f"Subagent {spec.id} failed: {e}")
            return f"Subagent error: {e}"
