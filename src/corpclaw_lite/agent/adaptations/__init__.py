"""Loop adaptations extracted from :class:`~corpclaw_lite.agent.loop.AgentLoop` (B-078).

Each module is a pure move of behavior-neutral helpers. Call order stays in
``AgentLoop.run`` (see ARCHITECTURE schema pipeline).
"""

from __future__ import annotations

from corpclaw_lite.agent.adaptations.closing import apply_closing_mode
from corpclaw_lite.agent.adaptations.finalize import auto_finalize_cascade
from corpclaw_lite.agent.adaptations.mandate import apply_workflow_mandate
from corpclaw_lite.agent.adaptations.tool_surface import (
    apply_tool_surface,
    inject_tool_soft_hint,
)

__all__ = [
    "apply_closing_mode",
    "apply_tool_surface",
    "apply_workflow_mandate",
    "auto_finalize_cascade",
    "inject_tool_soft_hint",
]
