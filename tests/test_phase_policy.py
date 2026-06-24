"""Tests for DefaultPhasePolicy (D-056 PR2).

Covers the decision matrix:
  1. Main agent default phase → None (no-op; routing sampling decides).
  2. Main agent closing_mode → thinking off.
  3. Research gathering (prev=gathering tools, no markers) → off.
  4. Research aggregation (prev=aggregation_markers) → default (on, → None).
  5. Research wall-clock fallback (nudge/restrict fired) → default (on, → None).
  6. Research first turn (prev=[]) → off (gathering).
  7. Disabled policy → always None.
  8. Custom markers/gathering tools + per-phase thinking config.
  9. closing_mode wins over research phase (budget pressure priority).
"""

from __future__ import annotations

from corpclaw_lite.agent.phase_policy import (
    DefaultPhasePolicy,
    PhaseContext,
    PhasePolicy,
)
from corpclaw_lite.config.settings import PhasePolicySettings

# Marker sets shared across tests (mirror the default PhasePolicySettings).
_GATHERING = frozenset(
    {
        "research_search",
        "research_fetch_source",
        "research_read_source",
        "research_list_sources",
        "research_store_fact",
    }
)
_AGGREGATION = frozenset({"research_list_facts"})


def _ctx(
    *,
    is_workflow_subagent: bool = False,
    iteration: int = 1,
    elapsed_ratio: float | None = None,
    closing_mode: bool = False,
    nudge_injected: bool = False,
    restricted: bool = False,
    prev_tool_calls: list[str] | None = None,
    tools_used: list[str] | None = None,
    aggregation_markers: frozenset[str] = _AGGREGATION,
    gathering_tools: frozenset[str] = _GATHERING,
) -> PhaseContext:
    return PhaseContext(
        is_workflow_subagent=is_workflow_subagent,
        iteration=iteration,
        elapsed_ratio=elapsed_ratio,
        closing_mode=closing_mode,
        nudge_injected=nudge_injected,
        restricted=restricted,
        prev_tool_calls=prev_tool_calls or [],
        tools_used=tools_used or [],
        aggregation_markers=aggregation_markers,
        gathering_tools=gathering_tools,
    )


def _policy(**overrides: object) -> DefaultPhasePolicy:
    return DefaultPhasePolicy(PhasePolicySettings(**overrides))  # type: ignore[arg-type]


def _thinking_mode(opts: object) -> str | None:
    """Extract the thinking mode from a RequestOptions (or None if None)."""
    if opts is None:
        return None
    th = getattr(opts, "thinking", None)
    return th.mode if th is not None else None


# ── Main agent ────────────────────────────────────────────────────────────────


def test_main_agent_default_phase_is_noop() -> None:
    """Main agent in its default phase returns None — no override."""
    policy = _policy()
    assert policy.options_for_phase(_ctx(is_workflow_subagent=False)) is None


def test_main_agent_closing_mode_thinking_off() -> None:
    """Main agent budget pressure (closing_mode) → thinking off."""
    policy = _policy()
    opts = policy.options_for_phase(_ctx(is_workflow_subagent=False, closing_mode=True))
    assert _thinking_mode(opts) == "off"


# ── Research subagent ─────────────────────────────────────────────────────────


def test_research_gathering_thinking_off() -> None:
    """Research gathering phase (prev turn called gathering tools) → off."""
    policy = _policy()
    opts = policy.options_for_phase(
        _ctx(is_workflow_subagent=True, prev_tool_calls=["research_search", "research_store_fact"])
    )
    assert _thinking_mode(opts) == "off"


def test_research_aggregation_thinking_default_on() -> None:
    """Research aggregation phase (prev turn called aggregation marker) → default.

    'default' produces NO override (None) so the model's natural thinking applies.
    """
    policy = _policy()
    opts = policy.options_for_phase(
        _ctx(is_workflow_subagent=True, prev_tool_calls=["research_list_facts"])
    )
    # aggregation_thinking default = "default" → no override.
    assert opts is None


def test_research_aggregation_with_off_config_returns_off() -> None:
    """If aggregation_thinking is configured 'off', aggregation phase → off."""
    policy = _policy(aggregation_thinking="off")
    opts = policy.options_for_phase(
        _ctx(is_workflow_subagent=True, prev_tool_calls=["research_list_facts"])
    )
    assert _thinking_mode(opts) == "off"


def test_research_wallclock_fallback_thinking_on() -> None:
    """Research: nudge fired (wall-clock) forces aggregation phase → default (on)."""
    policy = _policy()
    opts = policy.options_for_phase(
        _ctx(is_workflow_subagent=True, prev_tool_calls=["research_search"], nudge_injected=True)
    )
    # Even though prev turn was gathering, the wall-clock nudge forces aggregation.
    assert opts is None  # aggregation_thinking="default" → no override = natural thinking on


def test_research_wallclock_fallback_restricted() -> None:
    """Research: restrict fired (wall-clock) forces aggregation phase → default."""
    policy = _policy()
    opts = policy.options_for_phase(
        _ctx(is_workflow_subagent=True, prev_tool_calls=["research_search"], restricted=True)
    )
    assert opts is None


def test_research_first_turn_is_gathering_off() -> None:
    """Research first turn (prev_tool_calls empty) → gathering → off."""
    policy = _policy()
    opts = policy.options_for_phase(_ctx(is_workflow_subagent=True, prev_tool_calls=[]))
    assert _thinking_mode(opts) == "off"


# ── Monotonic gathering→aggregation transition (D-056 timing fix) ────────────


def test_research_aggregation_monotonic_after_list_facts() -> None:
    """Once list_facts was invoked in ANY prior turn, aggregation is sticky.

    The turn that calls research_finalize after list_facts must be in the
    aggregation phase (thinking on), even though list_facts is not in the
    immediately-previous turn — it's in the cumulative tools_used.
    """
    policy = _policy()
    # Prev turn = read_source (gathering tool), but list_facts already happened
    # earlier → cumulative has the marker → aggregation phase.
    opts = policy.options_for_phase(
        _ctx(
            is_workflow_subagent=True,
            prev_tool_calls=["research_read_source"],
            tools_used=["research_search", "research_list_facts", "research_read_source"],
        )
    )
    assert opts is None  # aggregation_thinking default → natural thinking on


def test_research_gathering_before_any_list_facts() -> None:
    """Pure gathering (no aggregation marker anywhere yet) → thinking off."""
    policy = _policy()
    opts = policy.options_for_phase(
        _ctx(
            is_workflow_subagent=True,
            prev_tool_calls=["research_store_fact"],
            tools_used=["research_search", "research_fetch_source", "research_store_fact"],
        )
    )
    assert _thinking_mode(opts) == "off"


def test_research_aggregation_sticky_after_finalize() -> None:
    """After research_finalize in cumulative, subsequent turns stay aggregation."""
    policy = _policy(aggregation_thinking="off")  # make override observable
    opts = policy.options_for_phase(
        _ctx(
            is_workflow_subagent=True,
            prev_tool_calls=["research_search"],  # gathering tool in prev
            tools_used=["research_finalize", "research_search"],
        )
    )
    # finalize in cumulative → aggregation phase (off, observable).
    assert _thinking_mode(opts) == "off"


def test_research_list_facts_turn_is_aggregation() -> None:
    """The turn right after a gathering turn, when list_facts was called, → aggregation.

    This covers the list_facts invocation turn's FOLLOW-UP: prev had list_facts,
    cumulative has list_facts → aggregation. (The list_facts call itself happens
    in a turn where prev was still gathering; that turn stays gathering by the
    before-the-call signal, but every turn after is aggregation.)
    """
    policy = _policy(aggregation_thinking="off")
    opts = policy.options_for_phase(
        _ctx(
            is_workflow_subagent=True,
            prev_tool_calls=["research_list_facts"],
            tools_used=["research_list_facts"],
        )
    )
    assert _thinking_mode(opts) == "off"  # aggregation phase


# ── Disabled policy ───────────────────────────────────────────────────────────


def test_disabled_policy_always_none() -> None:
    """When enabled=False, the policy never returns an override."""
    policy = _policy(enabled=False)
    assert policy.options_for_phase(_ctx(is_workflow_subagent=False, closing_mode=True)) is None
    assert policy.options_for_phase(_ctx(is_workflow_subagent=True)) is None
    assert (
        policy.options_for_phase(
            _ctx(is_workflow_subagent=True, prev_tool_calls=["research_list_facts"])
        )
        is None
    )


# ── Priority / conflict ───────────────────────────────────────────────────────


def test_closing_mode_wins_over_research_gathering() -> None:
    """Budget pressure (closing_mode) wins over research phase → off."""
    policy = _policy()
    opts = policy.options_for_phase(
        _ctx(
            is_workflow_subagent=True,
            closing_mode=True,
            prev_tool_calls=["research_list_facts"],  # aggregation marker present
            nudge_injected=True,
        )
    )
    # closing_mode is checked first → closing_thinking="off".
    assert _thinking_mode(opts) == "off"


# ── Custom config ─────────────────────────────────────────────────────────────


def test_custom_markers_and_gathering_tools() -> None:
    """Custom aggregation_markers / gathering_tools from settings are honored."""
    policy = _policy(
        aggregation_markers=["finalize", "summarize"],
        gathering_tools=["search", "fetch"],
    )
    # Custom aggregation marker → aggregation phase.
    opts = policy.options_for_phase(
        _ctx(
            is_workflow_subagent=True,
            prev_tool_calls=["summarize"],
            aggregation_markers=frozenset({"finalize", "summarize"}),
            gathering_tools=frozenset({"search", "fetch"}),
        )
    )
    assert opts is None  # aggregation_thinking default → no override

    # Default marker (research_list_facts) is NOT recognized with custom config.
    opts2 = policy.options_for_phase(
        _ctx(
            is_workflow_subagent=True,
            prev_tool_calls=["research_list_facts"],
            aggregation_markers=frozenset({"finalize", "summarize"}),
            gathering_tools=frozenset({"search", "fetch"}),
        )
    )
    assert _thinking_mode(opts2) == "off"  # treated as gathering (unknown tool → gathering)


def test_custom_closing_thinking_budget() -> None:
    """closing_thinking configurable to 'budget'."""
    policy = _policy(closing_thinking="budget")
    opts = policy.options_for_phase(_ctx(is_workflow_subagent=False, closing_mode=True))
    assert _thinking_mode(opts) == "budget"


# ── Protocol conformance ──────────────────────────────────────────────────────


def test_default_phase_policy_is_phase_policy_protocol() -> None:
    """DefaultPhasePolicy satisfies the runtime-checkable PhasePolicy Protocol."""
    policy = _policy()
    assert isinstance(policy, PhasePolicy)
