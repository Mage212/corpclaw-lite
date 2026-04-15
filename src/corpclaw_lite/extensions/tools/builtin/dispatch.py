from __future__ import annotations

from typing import TYPE_CHECKING, Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

__all__ = [
    "DispatchSubagentTool",
]

if TYPE_CHECKING:
    from corpclaw_lite.agent.subagent import SubagentDispatcher
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.users.models import User


class DispatchSubagentTool(Tool):
    """Allows the main agent to delegate tasks to specialized subagents."""

    name = "dispatch_subagent"
    description = (
        "Delegate a task to a specialized subagent. "
        "Use when the task requires specialized capabilities beyond available tools."
    )
    params = [
        ToolParam(
            name="subagent_id",
            type="string",
            description="The ID of the subagent to dispatch",
        ),
        ToolParam(
            name="task",
            type="string",
            description="The full task description for the subagent",
        ),
    ]
    risk_level = RiskLevel.LOW

    def __init__(
        self,
        dispatcher: SubagentDispatcher,
        subagent_registry: SubagentRegistry,
    ) -> None:
        self._dispatcher = dispatcher
        self._subagent_registry = subagent_registry

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        subagent_id = kwargs.get("subagent_id")
        task = kwargs.get("task")

        if not isinstance(subagent_id, str) or not isinstance(task, str):
            return "Error: 'subagent_id' and 'task' are required string parameters."

        if user is None:
            return "Error: User context is required for subagent dispatch."

        spec = self._subagent_registry.get_spec(subagent_id)
        if not spec:
            available = [s.id for s in self._subagent_registry.get_allowed_subagents(user)]
            return f"Error: Subagent '{subagent_id}' not found. Available: {available}"

        # Department-level permission check on the subagent spec
        if "*" not in spec.allowed_departments and user.department not in spec.allowed_departments:
            return (
                f"Error: Permission denied. Your department ({user.department}) "
                f"cannot use subagent '{subagent_id}'."
            )

        return await self._dispatcher.dispatch(spec, user, task)
