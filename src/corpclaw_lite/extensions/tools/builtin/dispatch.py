from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

__all__ = [
    "DispatchSubagentTool",
]

if TYPE_CHECKING:
    from corpclaw_lite.agent.subagent import SubagentDispatcher
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.llm.queue import LLMQueueStatus
    from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)


class DispatchSubagentTool(Tool):
    """Allows the main agent to delegate tasks to specialized subagents."""

    name = "dispatch_subagent"
    description = (
        "Delegate a task to a specialized subagent. "
        "Use when the task requires specialized capabilities beyond available tools. "
        "For web research, fact-checking, source comparison, or URL analysis, use "
        "subagent_id='research-agent' and pass the full research question."
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
        permission_checker: PermissionChecker | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._subagent_registry = subagent_registry
        self._permission_checker = permission_checker

    def _available_subagent_ids(self, user: User) -> list[str]:
        return [
            spec.id
            for spec in self._subagent_registry.get_allowed_subagents(user)
            if (
                self._permission_checker is None
                or self._permission_checker.can_dispatch_subagent(user, spec.id)
            )
        ]

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        subagent_id = kwargs.get("subagent_id")
        task = kwargs.get("task")
        run_id = kwargs.get("run_id")
        parent_run_id = run_id if isinstance(run_id, str) else None
        raw_on_subagent_tool_start = kwargs.get("on_subagent_tool_start")
        raw_on_subagent_tool_batch_start = kwargs.get("on_subagent_tool_batch_start")
        raw_on_subagent_llm_stage = kwargs.get("on_subagent_llm_stage")
        raw_on_subagent_llm_queue_status = kwargs.get("on_subagent_llm_queue_status")
        on_subagent_tool_start = (
            cast("Callable[[str, str], None]", raw_on_subagent_tool_start)
            if callable(raw_on_subagent_tool_start)
            else None
        )
        on_subagent_tool_batch_start = (
            cast("Callable[[str, list[str]], None]", raw_on_subagent_tool_batch_start)
            if callable(raw_on_subagent_tool_batch_start)
            else None
        )
        on_subagent_llm_stage = (
            cast("Callable[[str, str], None]", raw_on_subagent_llm_stage)
            if callable(raw_on_subagent_llm_stage)
            else None
        )
        on_subagent_llm_queue_status = (
            cast("Callable[[str, LLMQueueStatus], None]", raw_on_subagent_llm_queue_status)
            if callable(raw_on_subagent_llm_queue_status)
            else None
        )

        if not isinstance(subagent_id, str) or not isinstance(task, str):
            return "Error: 'subagent_id' and 'task' are required string parameters."

        if user is None:
            return "Error: User context is required for subagent dispatch."

        logger.info(
            "Delegation request: subagent=%s user=%s task=%.80s",
            subagent_id,
            user.id,
            task,
        )

        spec = self._subagent_registry.get_spec(subagent_id)
        if not spec:
            available = self._available_subagent_ids(user)
            logger.warning(
                "Subagent not found: requested=%s user=%s available=%s",
                subagent_id,
                user.id,
                available,
            )
            return f"Error: Subagent '{subagent_id}' not found. Available: {available}"

        if (
            self._permission_checker is not None
            and not self._permission_checker.can_dispatch_subagent(user, subagent_id)
        ):
            logger.warning(
                "Permission denied by department RBAC: subagent=%s user=%s dept=%s",
                subagent_id,
                user.id,
                user.department,
            )
            return (
                f"Error: Permission denied. Your department ({user.department}) "
                f"cannot dispatch subagent '{subagent_id}'."
            )

        # Department-level permission check on the subagent spec
        if "*" not in spec.allowed_departments and user.department not in spec.allowed_departments:
            logger.warning(
                "Permission denied: subagent=%s user=%s dept=%s allowed=%s",
                subagent_id,
                user.id,
                user.department,
                spec.allowed_departments,
            )
            return (
                f"Error: Permission denied. Your department ({user.department}) "
                f"cannot use subagent '{subagent_id}'."
            )

        return await self._dispatcher.dispatch(
            spec,
            user,
            task,
            parent_run_id=parent_run_id,
            on_subagent_tool_start=on_subagent_tool_start,
            on_subagent_tool_batch_start=on_subagent_tool_batch_start,
            on_subagent_llm_stage=on_subagent_llm_stage,
            on_subagent_llm_queue_status=on_subagent_llm_queue_status,
        )

    def should_return_direct(self, arguments: dict[str, Any], result: str) -> bool:
        subagent_id = arguments.get("subagent_id")
        if not isinstance(subagent_id, str):
            return False
        spec = self._subagent_registry.get_spec(subagent_id)
        return bool(
            spec is not None
            and spec.direct_response
            and not result.startswith("Subagent error:")
            and not result.startswith("Error")
        )
