"""B-046 soft-deadline closing mode (adaptation layer).

Moved from :class:`~corpclaw_lite.agent.loop.AgentLoop` (B-078 / PR 2A.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from corpclaw_lite.logging.trace import log_event

if TYPE_CHECKING:
    from corpclaw_lite.agent.guards import SoftDeadline
    from corpclaw_lite.agent.loop import RunStats
    from corpclaw_lite.agent.task_run import TaskRun
    from corpclaw_lite.users.models import User

__all__ = [
    "apply_closing_mode",
    "narrow_schema_to_terminal",
]


def narrow_schema_to_terminal(
    schema: list[dict[str, Any]] | None,
    terminal_tool_names: frozenset[str],
) -> list[dict[str, Any]] | None:
    """Keep only tools marked terminal in the registry."""
    if not schema:
        return schema
    if not terminal_tool_names:
        return schema
    return [s for s in schema if str(s.get("function", {}).get("name", "")) in terminal_tool_names]


async def apply_closing_mode(
    soft_deadline: SoftDeadline,
    tools_schema: list[dict[str, Any]] | None,
    task_run: TaskRun,
    user: User,
    stats: RunStats,
    *,
    terminal_tool_names: frozenset[str],
    max_wall_time_ms: int,
    soft_deadline_ratio: float,
) -> list[dict[str, Any]] | None:
    """Enter closing mode when the wall-clock soft deadline is reached.

    Closing mode reduces ``tools_schema`` to terminal tools only so the model is
    pushed to wrap up instead of being hard-cancelled by ``asyncio.wait_for``.
    Idempotent: once closing mode is entered the schema stays reduced and the
    deadline/event are only emitted once. Returns the (possibly reduced) schema.

    B-046: called both at the top of each iteration and immediately before each LLM
    provider call, so a single long iteration that straddles the deadline still
    triggers the reduction before the model is asked for more tool calls.

    Async because ``task_run.mark_soft_deadline`` writes to disk off the event loop.
    """
    # Already in closing mode: re-apply terminal filter. B-107 rebuilds schema
    # from base each iteration; without this re-narrow, closing would be undone.
    if soft_deadline.closing_mode:
        return narrow_schema_to_terminal(tools_schema, terminal_tool_names)
    if not soft_deadline.is_reached():
        return tools_schema
    soft_deadline.enter_closing_mode()
    await task_run.mark_soft_deadline(user, stats.run_id)
    log_event(
        "agent_soft_deadline_reached",
        stats.run_id,
        max_time_ms=max_wall_time_ms,
        ratio=soft_deadline_ratio,
    )
    return narrow_schema_to_terminal(tools_schema, terminal_tool_names)
