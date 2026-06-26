"""Depth mode resolution for the agent loop (Etap 3, Sprint 3A).

The UI exposes a depth selector (Fast/Think) orthogonal to the Chat/Work
section (tools on/off). Each depth resolves to a named ``SamplingProfile`` in
``config/model_presets.yaml`` keyed by the route's model, so every model can
carry its own official inference/thinking parameters. The resolution feeds
into ``LLMRouter.with_overrides(sampling_name=...)`` so the full profile
(thinking_mode + inference_overrides) is applied — no raw thinking override
that bypasses the preset system (spec §7).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from corpclaw_lite.config.settings import DepthModeSettings
    from corpclaw_lite.llm.presets import PresetRegistry

__all__ = ["DepthMode", "resolve_depth_sampling"]

logger = logging.getLogger(__name__)

# User-selectable processing depth. "research" is added in Sprint 3B.
DepthMode = Literal["fast", "think"]


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
    """
    mapping = settings.fast if depth == "fast" else settings.think
    name = mapping.get(route_model)
    if not name:
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
