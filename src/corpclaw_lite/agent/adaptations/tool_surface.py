"""B-107 tool-surface phase filter + BM25 soft-hint (adaptation layer).

Moved from :class:`~corpclaw_lite.agent.loop.AgentLoop` (B-078 / PR 2A.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from corpclaw_lite.agent.tool_surface import (
    ToolSurfaceProfile,
    apply_phase_filter,
    detect_tool_surface_phase,
    inject_soft_hint,
    rank_tool_names,
)
from corpclaw_lite.logging.trace import log_event

if TYPE_CHECKING:
    from corpclaw_lite.agent.loop_state import LoopState
    from corpclaw_lite.config.settings import ToolSurfaceSettings

__all__ = [
    "apply_tool_surface",
    "inject_tool_soft_hint",
]


def apply_tool_surface(
    state: LoopState,
    *,
    user_message: str,
    settings: ToolSurfaceSettings,
    profile: str,
) -> None:
    """Phase-filter schema from base; re-apply mandate restrict (H1).

    Mutates ``state.tools_schema`` and ``state.tool_surface_phase``.
    True no-op when tool_surface disabled (preserves mandate/closing mutations).
    """
    ts_cfg = settings
    if not ts_cfg.enabled:
        state.tools_schema = state.mandate.apply_schema_restrict(state.tools_schema)
        return

    resolved: ToolSurfaceProfile = (
        profile  # type: ignore[assignment]
        if profile in ("main", "office", "execution", "none")
        else "main"
    )

    # Research mandate owns the funnel — hard filter off.
    hard_enabled = (
        ts_cfg.enabled and resolved in ts_cfg.hard_filter_profiles and not state.mandate.enabled
    )

    phase = detect_tool_surface_phase(
        user_message=user_message,
        tools_used=state.stats.tools_used,
        prev_phase=state.tool_surface_phase,
        closing_mode=state.soft_deadline.closing_mode,
        mandate_enabled=state.mandate.enabled,
        profile=resolved,
    )

    if hard_enabled and phase is not None:
        prev = state.tool_surface_phase
        phase_str = str(phase)
        if prev != phase_str:
            state.tool_surface_phase = phase_str
            log_event(
                "tool_surface_phase_changed",
                state.stats.run_id,
                iteration=state.stats.iterations,
                phase=phase_str,
                prev_phase=prev,
                profile=resolved,
            )
        state.tools_schema = apply_phase_filter(
            state.base_tools_schema,
            phase,
            profile=resolved,
            enabled=True,
        )
    else:
        # Start from full base so closing-mode / mandate can re-narrow cleanly.
        state.tools_schema = (
            list(state.base_tools_schema) if state.base_tools_schema is not None else None
        )

    # H1: re-apply mandate restrict after any base rebuild.
    state.tools_schema = state.mandate.apply_schema_restrict(state.tools_schema)


def inject_tool_soft_hint(
    state: LoopState,
    *,
    user_message: str,
    settings: ToolSurfaceSettings,
    profile: str,
) -> None:
    """One soft-hint in messages tail (cache-safe). Call only pre-LLM."""
    ts_cfg = settings
    if not ts_cfg.enabled or not ts_cfg.soft_hint_enabled:
        return
    if profile in ("none", "execution"):
        return
    ranked = rank_tool_names(
        user_message,
        state.tools_schema,
        top_k=ts_cfg.soft_hint_top_k,
    )
    state.context.messages = inject_soft_hint(
        state.context.messages,
        ranked,
        enabled=True,
    )
