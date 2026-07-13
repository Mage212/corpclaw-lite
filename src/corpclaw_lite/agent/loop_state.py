"""Per-run mutable state for :class:`~corpclaw_lite.agent.loop.AgentLoop` (B-076).

Makes the coupling between loop adaptations (closing-mode, mandate, phase-policy,
future tool-surface) **explicit** by holding shared runtime objects on one
dataclass instead of a cloud of locals inside ``run()``.

Behavior-neutral: this is a pure structure extraction (DC-001 Phase 1). Logic
stays in ``loop.py``; B-107 will hang tool-surface fields off this bag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from corpclaw_lite.agent.context import ContextBuilder
    from corpclaw_lite.agent.guards import (
        PlanningTextGuard,
        ResultDedupGuard,
        SimpleBudgetGuard,
        SimpleProgressGuard,
        SoftDeadline,
        TerminalToolMandate,
    )
    from corpclaw_lite.agent.loop import RunStats
    from corpclaw_lite.agent.task_run import TaskRun

__all__ = [
    "LoopState",
]


@dataclass
class LoopState:
    """Mutable per-run state owned by ``AgentLoop.run``.

    Collaborators on ``AgentLoop`` itself (provider, registry, settings,
    compressor) stay on ``self`` — only run-scoped mutable pieces live here.
    """

    stats: RunStats
    budget: SimpleBudgetGuard
    progress: SimpleProgressGuard
    result_dedup: ResultDedupGuard
    planning_guard: PlanningTextGuard
    soft_deadline: SoftDeadline
    mandate: TerminalToolMandate
    context: ContextBuilder
    # Immutable source of truth for tool schemas for this run (B-107 refilters
    # from this; closing/mandate only narrow a derived copy).
    base_tools_schema: list[dict[str, Any]] | None
    tools_schema: list[dict[str, Any]] | None
    task_run: TaskRun
    mem_key: str
    prev_turn_tools: list[str] = field(default_factory=lambda: [])
    current_turn_tools: list[str] = field(default_factory=lambda: [])
    last_actual_total_tokens: int | None = None
    # B-107: optional tool-surface phase (str to avoid import cycle; module
    # tool_surface will set ToolSurfacePhase values). None = not tracked yet.
    tool_surface_phase: str | None = None
