"""Phase-based per-call thinking overrides (D-056 PR2).

A :class:`PhasePolicy` inspects the current task phase and returns a
:class:`~corpclaw_lite.llm.base.RequestOptions` (per-call override) that is set
on the ``RequestOptions`` contextvar for the duration of a single LLM call. The
provider merges it with the model/sampling profiles (priority: model profile <
sampling < RequestOptions < transport), so a policy can flip thinking on/off or
cap its budget for one call without touching routing config or rebuilding
provider instances.

The default policy (:class:`DefaultPhasePolicy`) implements two task patterns:

- **Main agent closing mode** — when the wall-clock soft deadline is reached
  (budget pressure), thinking is turned off to force a direct final answer
  instead of a long reasoning chain that may time out.

- **Workflow subagent (research)** — gathering vs aggregation phase. Gathering
  (still collecting sources) runs with thinking off for speed; aggregation
  (about to write the final report) runs with thinking on for synthesis
  quality. Phase is detected semantically first (the previous turn called an
  aggregation marker such as ``research_list_facts``), with a wall-clock
  fallback (``TerminalToolMandate`` nudge/restrict) in case the model never
  reaches the marker on a slow run.

The policy is enabled by default but is a no-op for the main agent in its
default phase (returns ``None``), so enabling it does not change main-agent
behaviour unless the budget runs out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from corpclaw_lite.config.settings import PhasePolicySettings
from corpclaw_lite.llm.base import RequestOptions, ThinkingOverride

__all__ = [
    "DefaultPhasePolicy",
    "PhaseContext",
    "PhasePolicy",
]


@dataclass(frozen=True)
class PhaseContext:
    """Read-only snapshot of the loop state at the LLM-call site.

    Built by :class:`~corpclaw_lite.agent.loop.AgentLoop` right before each LLM
    call, after closing-mode is applied and before the provider dispatch. The
    policy reads only this snapshot — it has no handle on the loop itself,
    keeping the decision pure and testable.
    """

    # True when a terminal tool is configured (workflow subagent such as
    # research-agent). ``mandate.enabled`` is the canonical signal.
    is_workflow_subagent: bool
    iteration: int
    # Wall-clock fraction of max_wall_time_ms. None when the mandate is
    # disabled (main agent). From ``mandate.elapsed_ratio()``.
    elapsed_ratio: float | None
    # True once the wall-clock soft deadline (default 0.85) is reached.
    closing_mode: bool
    # One-shot mandate flags (research subagent). True after the nudge/restrict
    # escalation fired (default at 0.6 / 0.75 of budget). Used as the wall-clock
    # fallback to detect a forced aggregation phase.
    nudge_injected: bool
    restricted: bool
    # Tool names invoked in the PREVIOUS turn (per-turn, not cumulative).
    # Empty on the first turn. The semantic primary signal.
    prev_tool_calls: list[str]
    # Configured marker sets (from PhasePolicySettings).
    aggregation_markers: frozenset[str]
    gathering_tools: frozenset[str]


@runtime_checkable
class PhasePolicy(Protocol):
    """Inspects task phase and returns per-call RequestOptions, or None."""

    def options_for_phase(self, ctx: PhaseContext) -> RequestOptions | None:
        """Return a RequestOptions override for this LLM call, or None.

        Returning ``None`` means "do not override" — the routing-resolved
        model/sampling profiles decide thinking as usual.
        """
        ...


def _thinking_options(mode: str) -> RequestOptions | None:
    """Build RequestOptions for a thinking mode, or None for 'default'.

    'default' produces no override so the model's natural thinking applies —
    returning None keeps the contextvar unset and the provider uses its
    sampling profile unchanged.
    """
    if mode == "default":
        return None
    return RequestOptions(thinking=ThinkingOverride(mode=mode))  # type: ignore[arg-type]


class DefaultPhasePolicy:
    """Built-in phase policy driven by :class:`PhasePolicySettings`.

    Decision order (first match wins):

    1. ``closing_mode`` → ``closing_thinking`` (main agent budget pressure, and
       also a workflow subagent whose budget is running out).
    2. Workflow subagent, aggregation marker in previous turn →
       ``aggregation_thinking`` (semantic primary).
    3. Workflow subagent, nudge/restrict fired → ``aggregation_thinking``
       (wall-clock fallback — model is being forced to finalize).
    4. Workflow subagent, otherwise (gathering, incl. first turn) →
       ``gathering_thinking``.
    5. Main agent default phase → ``None`` (no override; routing sampling
       decides thinking).
    """

    def __init__(self, settings: PhasePolicySettings) -> None:
        self._enabled = settings.enabled
        self._closing_thinking = settings.closing_thinking
        self._gathering_thinking = settings.gathering_thinking
        self._aggregation_thinking = settings.aggregation_thinking

    def options_for_phase(self, ctx: PhaseContext) -> RequestOptions | None:
        if not self._enabled:
            return None

        # 1. Closing mode — budget pressure wins for both main agent and
        #    workflow subagent (force a final answer, skip reasoning).
        if ctx.closing_mode:
            return _thinking_options(self._closing_thinking)

        # 2-4. Workflow subagent (research): semantic primary + wall-clock fallback.
        if ctx.is_workflow_subagent:
            prev_set = set(ctx.prev_tool_calls)
            # Aggregation: previous turn reviewed facts → synthesis phase now.
            if ctx.aggregation_markers & prev_set:
                return _thinking_options(self._aggregation_thinking)
            # Wall-clock fallback: mandate nudged/restricted toward finalization.
            if ctx.nudge_injected or ctx.restricted:
                return _thinking_options(self._aggregation_thinking)
            # Gathering (incl. first turn where prev_tool_calls is empty).
            return _thinking_options(self._gathering_thinking)

        # 5. Main agent default phase — no override.
        return None
