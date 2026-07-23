"""Task-local runtime context for tool execution.

Runtime metadata is deliberately kept outside ``Tool.execute(**kwargs)`` so
plugin and MCP tools receive only the business arguments declared in their
schemas.  ``ContextVar`` keeps concurrent agent runs isolated while preserving
the public ``Tool.execute`` signature.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from corpclaw_lite.llm.queue import LLMQueueStatus
    from corpclaw_lite.users.models import User

__all__ = [
    "ToolExecutionContext",
    "bind_tool_execution_context",
    "get_tool_execution_context",
]


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Non-serializable metadata associated with one registry execution."""

    user: User | None = None
    run_id: str | None = None
    on_subagent_tool_start: Callable[[str, str], None] | None = None
    on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None = None
    on_subagent_llm_stage: Callable[[str, str], None] | None = None
    on_subagent_llm_queue_status: Callable[[str, LLMQueueStatus], None] | None = None
    parent_trajectory_recorder: Any | None = None


_TOOL_EXECUTION_CONTEXT: ContextVar[ToolExecutionContext | None] = ContextVar(
    "tool_execution_context",
    default=None,
)


def get_tool_execution_context() -> ToolExecutionContext | None:
    """Return metadata for the current tool call, if called through a registry."""

    return _TOOL_EXECUTION_CONTEXT.get()


@contextmanager
def bind_tool_execution_context(context: ToolExecutionContext) -> Generator[None]:
    """Bind *context* for one call and restore the prior value reliably."""

    token = _TOOL_EXECUTION_CONTEXT.set(context)
    try:
        yield
    finally:
        _TOOL_EXECUTION_CONTEXT.reset(token)
