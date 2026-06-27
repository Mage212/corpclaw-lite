"""Depth mode resolution for the agent loop (Etap 3, Sprint 3A + 3B).

The UI exposes a depth selector (Fast/Think/Research) orthogonal to the
Chat/Work section (tools on/off). Each depth resolves to a named
``SamplingProfile`` in ``config/model_presets.yaml`` keyed by the route's
model, so every model can carry its own official inference/thinking parameters.
The resolution feeds into ``LLMRouter.with_overrides(sampling_name=...)`` so
the full profile (thinking_mode + inference_overrides) is applied — no raw
thinking override that bypasses the preset system (spec §7).

Sprint 3B adds ``"research"``: it forces ``deep_research`` mode in the
research subagent (bypassing keyword detection). The depth mode is threaded
from ``AgentLoop.run()`` down to ``DispatchSubagentTool`` via a contextvar so
the LLM-facing tool schema is unchanged.
"""

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from corpclaw_lite.config.settings import DepthModeSettings
    from corpclaw_lite.llm.presets import PresetRegistry

__all__ = [
    "DepthMode",
    "get_call_depth_mode",
    "reset_call_depth_mode",
    "resolve_depth_sampling",
    "set_call_depth_mode",
]

logger = logging.getLogger(__name__)

# User-selectable processing depth.
# - "fast" / "think": thinking off/on via sampling profiles (Sprint 3A).
# - "research": forces deep_research in the research subagent (Sprint 3B).
DepthMode = Literal["fast", "think", "research"]

# Etap 3B: per-run contextvar carrying the user-selected depth mode from
# AgentLoop.run() down to DispatchSubagentTool (which reads it to force
# deep_research). Scoped per asyncio task, so concurrent runs are isolated.
# Subagents start their own AgentLoop.run() with depth_mode=None, so the
# contextvar does not leak "research" into nested subagent dispatches.
_call_depth_mode: contextvars.ContextVar[DepthMode | None] = contextvars.ContextVar(
    "corpclaw_call_depth_mode", default=None
)


def set_call_depth_mode(mode: DepthMode | None) -> contextvars.Token[DepthMode | None]:
    """Set the per-run depth mode contextvar. Returns a token for ``reset``."""
    return _call_depth_mode.set(mode)


def reset_call_depth_mode(token: contextvars.Token[DepthMode | None]) -> None:
    """Reset the per-run depth mode contextvar to its prior value."""
    _call_depth_mode.reset(token)


def get_call_depth_mode() -> DepthMode | None:
    """Read the per-run depth mode (None when no depth override is active)."""
    return _call_depth_mode.get()


def resolve_depth_sampling(
    depth: DepthMode,
    route_model: str,
    settings: DepthModeSettings,
    preset_registry: PresetRegistry | None,
) -> str | None:
    """Resolve a sampling-profile name for the given depth + route model.

    Returns ``None`` when:
      - no mapping is configured for ``(depth, route_model)``, or
      - the named profile does not exist in ``preset_registry`` (warn + ignore).

    The caller falls back to the route's default sampling profile when ``None``
    is returned, so a missing depth mapping never breaks agent execution.

    Note: "research" intentionally returns None here — it affects only the
    subagent dispatcher (forced deep_research via contextvar), NOT the main
    agent's sampling profile. The main agent keeps its route default in research
    depth; only the research subagent's behaviour changes.
    """
    if depth == "research":
        return None
    mapping = settings.fast if depth == "fast" else settings.think
    name = mapping.get(route_model)
    if not name:
        logger.warning(
            "Depth mode '%s' has no sampling mapping for model '%s'; using route default.",
            depth,
            route_model,
        )
        return None
    if preset_registry is not None and preset_registry.get_sampling_profile(name) is None:
        logger.warning(
            "Depth mode '%s' references unknown sampling profile '%s' for model '%s'; "
            "falling back to route default.",
            depth,
            name,
            route_model,
        )
        return None
    return name
