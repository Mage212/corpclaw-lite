"""B-118: schedule tools — propose only (accept is human CLI/API)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.scheduler.service import SchedulerError, SchedulerService

__all__ = [
    "ScheduleCancelTool",
    "ScheduleListTool",
    "SchedulePauseTool",
    "ScheduleProposeTool",
    "ScheduleResumeTool",
]

if TYPE_CHECKING:
    from corpclaw_lite.users.models import User


class ScheduleProposeTool(Tool):
    """Propose a scheduled agent task (pending until human accepts)."""

    name = "schedule_propose"
    description = (
        "Propose a recurring or one-shot agent task for later. "
        "Does NOT activate the schedule — the user must confirm. "
        "Use schedule_text like 'every 1d', '0 9 * * 1-5', '2h', or ISO datetime. "
        "Max 3 pending+active tasks per user."
    )
    params = [
        ToolParam(
            name="title",
            type="string",
            description="Short title for the task",
        ),
        ToolParam(
            name="task_text",
            type="string",
            description="Full instruction the agent will execute when due",
        ),
        ToolParam(
            name="schedule_text",
            type="string",
            description="When to run: 'every 2h', '0 9 * * *', '30m', ISO datetime",
        ),
    ]
    risk_level = RiskLevel.MEDIUM
    parallel_safe = False

    def __init__(self, scheduler: SchedulerService) -> None:
        self._scheduler = scheduler

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required for schedule_propose."
        title = kwargs.get("title")
        task_text = kwargs.get("task_text")
        schedule_text = kwargs.get("schedule_text")
        if not isinstance(title, str) or not isinstance(task_text, str):
            return "Error: 'title' and 'task_text' are required strings."
        if not isinstance(schedule_text, str):
            return "Error: 'schedule_text' is required string."
        try:
            task = await self._scheduler.propose(
                user,
                title=title,
                task_text=task_text,
                schedule_text=schedule_text,
            )
        except SchedulerError as exc:
            return f"Error: {exc}"
        return (
            f"Proposed schedule task id={task.id} status=pending. "
            f"User must accept before it runs. "
            f"schedule_kind={task.schedule.kind}."
        )


class ScheduleListTool(Tool):
    """List the user's scheduled tasks."""

    name = "schedule_list"
    description = "List this user's pending, active, and paused scheduled tasks."
    params: list[ToolParam] = []
    risk_level = RiskLevel.LOW

    def __init__(self, scheduler: SchedulerService) -> None:
        self._scheduler = scheduler

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required for schedule_list."
        tasks = await self._scheduler.list_for_user(user, statuses=["pending", "active", "paused"])
        if not tasks:
            return "No scheduled tasks."
        lines = [
            (
                f"- id={t.id} status={t.status} title={t.title!r} "
                f"when={t.schedule_text!r} next_run_at={t.next_run_at}"
            )
            for t in tasks
        ]
        return "\n".join(lines)


class ScheduleCancelTool(Tool):
    """Dismiss/cancel a pending or active scheduled task."""

    name = "schedule_cancel"
    description = "Cancel (dismiss) one of the user's scheduled tasks by id."
    params = [
        ToolParam(name="task_id", type="string", description="Task id from schedule_list"),
    ]
    risk_level = RiskLevel.MEDIUM
    parallel_safe = False

    def __init__(self, scheduler: SchedulerService) -> None:
        self._scheduler = scheduler

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required."
        task_id = kwargs.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return "Error: 'task_id' is required."
        try:
            task = await self._scheduler.dismiss(user, task_id.strip())
        except SchedulerError as exc:
            return f"Error: {exc}"
        return f"Dismissed task id={task.id}."


class SchedulePauseTool(Tool):
    """Pause an active scheduled task."""

    name = "schedule_pause"
    description = "Pause an active scheduled task (will not fire until resumed)."
    params = [
        ToolParam(name="task_id", type="string", description="Task id"),
    ]
    risk_level = RiskLevel.LOW
    parallel_safe = False

    def __init__(self, scheduler: SchedulerService) -> None:
        self._scheduler = scheduler

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required."
        task_id = kwargs.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return "Error: 'task_id' is required."
        try:
            task = await self._scheduler.pause(user, task_id.strip())
        except SchedulerError as exc:
            return f"Error: {exc}"
        return f"Paused task id={task.id}."


class ScheduleResumeTool(Tool):
    """Resume a paused scheduled task."""

    name = "schedule_resume"
    description = "Resume a paused scheduled task."
    params = [
        ToolParam(name="task_id", type="string", description="Task id"),
    ]
    risk_level = RiskLevel.LOW
    parallel_safe = False

    def __init__(self, scheduler: SchedulerService) -> None:
        self._scheduler = scheduler

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required."
        task_id = kwargs.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return "Error: 'task_id' is required."
        try:
            task = await self._scheduler.resume(user, task_id.strip())
        except SchedulerError as exc:
            return f"Error: {exc}"
        return f"Resumed task id={task.id} next_run_at={task.next_run_at}."
