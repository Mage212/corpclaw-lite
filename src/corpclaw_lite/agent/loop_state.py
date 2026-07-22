"""Per-run mutable state for :class:`~corpclaw_lite.agent.loop.AgentLoop` (B-076 / B-077).

Makes the coupling between loop adaptations (closing-mode, mandate, phase-policy,
tool-surface) **explicit** by holding shared runtime objects on one dataclass
instead of a cloud of locals inside ``run()``.

B-077 adds :class:`TurnTokens` (contextvar reset handles for epilogue) and folds
run counters (``t0``, loop-warning / empty-response retries) into :class:`LoopState`.

Behavior-neutral structure extraction (DC-001 Phase 1–2). Logic stays in
``loop.py``; B-078 will move adaptations into ``agent/adaptations/``.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from corpclaw_lite.agent.context import ContextBuilder
    from corpclaw_lite.agent.context_target import ContextTargetTokens
    from corpclaw_lite.agent.depth_mode import DepthMode
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
    "TurnTokens",
]


@dataclass
class TurnTokens:
    """Contextvar reset handles for one :meth:`~corpclaw_lite.agent.loop.AgentLoop.run`.

    Populated by prologue (:meth:`~corpclaw_lite.agent.loop.AgentLoop._build_turn_context`);
    cleared by epilogue (:meth:`~corpclaw_lite.agent.loop.AgentLoop._finalize_turn`).
    Partial fills are safe: ``_finalize_turn`` only resets non-``None`` fields.
    """

    depth: contextvars.Token[DepthMode | None] | None = None
    context_target: ContextTargetTokens | None = None
    capture: tuple[Any, Any] | None = None
    run_id: Any = None
    workspace: contextvars.Token[Path | None] | None = None
    # B-091: main-agent web_fetch allow/deny for this run.
    web_access: contextvars.Token[bool] | None = None
    # True only after health.increment("active_requests") in prologue — so epilogue
    # does not under-count when build fails before that point.
    active_request_counted: bool = False


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
    # Regenerated persisted-user data is provider input for this run only.  The
    # durable store intentionally contains the raw user message, so store-first
    # compression must restore this envelope in memory before the next LLM call.
    ephemeral_user_message: str | None = None
    durable_user_message: str | None = None
    prev_turn_tools: list[str] = field(default_factory=lambda: [])
    current_turn_tools: list[str] = field(default_factory=lambda: [])
    last_actual_total_tokens: int | None = None
    # B-107: optional tool-surface phase (str to avoid import cycle; module
    # tool_surface will set ToolSurfacePhase values). None = not tracked yet.
    tool_surface_phase: str | None = None
    # B-077: run wall-clock start and loop-local counters (was bare locals in run()).
    t0: float = 0.0
    loop_warning_count: int = 0
    xml_repair_attempted: bool = False
    empty_response_retries: int = 0
    # B-118 H2: channel for this run (e.g. "system" headless) — execute-time denylist.
    channel: str | None = None
