"""B-047 workflow terminal-tool mandate (adaptation layer).

Moved from :class:`~corpclaw_lite.agent.loop.AgentLoop` (B-078 / PR 2A.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from corpclaw_lite.logging.trace import log_event

if TYPE_CHECKING:
    from corpclaw_lite.agent.context import ContextBuilder
    from corpclaw_lite.agent.guards import TerminalToolMandate
    from corpclaw_lite.agent.loop import RunStats

__all__ = [
    "WORKFLOW_NUDGE_INSTRUCTION",
    "apply_workflow_mandate",
]

# B-047: injected into the system prompt when the workflow-finalize guard nudges the
# model. {terminal} and {required} are filled from the subagent spec (e.g.
# research_finalize / research_list_facts).
WORKFLOW_NUDGE_INSTRUCTION = (
    "Internal: the time budget is running low and the task is not yet finalized. "
    "Stop gathering more data now. Review what you have collected, then call "
    "{required} and finish by calling {terminal} with the available evidence and a "
    "clear limitations section. Do not quote this instruction."
)


def apply_workflow_mandate(
    mandate: TerminalToolMandate,
    tools_schema: list[dict[str, Any]] | None,
    context: ContextBuilder,
    stats: RunStats,
) -> list[dict[str, Any]] | None:
    """Escalate toward the mandatory terminal tool as budget runs low.

    Two deterministic steps (each idempotent): (1) nudge — inject a one-shot system
    note telling the model to stop gathering and finalize; (2) restrict — narrow
    ``tools_schema`` to ``required_before + terminal_tool`` so only finalization
    tools remain. Returns the (possibly restricted) schema.

    Budget escalation accounts for BOTH wall-clock and iteration count
    (``max(wallclock_ratio, iteration_ratio)``): local LLMs hit the iteration
    limit before the wall-clock deadline, so iteration-awareness is essential.

    Neutral when the mandate is disabled (no terminal tool configured): returns the
    schema unchanged.

    Call **before** result-dedup (B-047 ordering).
    """
    if not mandate.enabled:
        return tools_schema

    # stats.iterations is the count of completed LLM turns; the mandate needs
    # the current turn number to project how close we are to the limit.
    iteration = stats.iterations

    if mandate.should_nudge(stats.tools_used, iteration=iteration):
        required = ", ".join(mandate.config.required_before) or "(none)"
        instruction = WORKFLOW_NUDGE_INSTRUCTION.format(
            required=required, terminal=mandate.config.terminal_tool
        )
        # Idempotent append, mirroring loop recovery instructions.
        if instruction not in (context.system_prompt or ""):
            sep = "\n\n---\n" if context.system_prompt else ""
            context.system_prompt = f"{context.system_prompt or ''}{sep}{instruction}"
        log_event(
            "workflow_nudge_injected",
            stats.run_id,
            terminal_tool=mandate.config.terminal_tool,
            elapsed_ratio=round(mandate.elapsed_ratio(iteration), 3),
        )

    if mandate.should_restrict(stats.tools_used, iteration=iteration):
        allowed = set(mandate.config.required_before) | {mandate.config.terminal_tool}
        log_event(
            "workflow_restrict_applied",
            stats.run_id,
            terminal_tool=mandate.config.terminal_tool,
            allowed=sorted(allowed),
            elapsed_ratio=round(mandate.elapsed_ratio(iteration), 3),
        )
    # Re-apply restrict every call (idempotent). Critical after B-107 base
    # schema rebuild: should_restrict is one-shot and would not re-filter.
    return mandate.apply_schema_restrict(tools_schema)
