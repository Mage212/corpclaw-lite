"""Tests for B-047: TerminalToolMandate workflow-finalize guard."""

from __future__ import annotations

import time

from corpclaw_lite.agent.guards import (
    TerminalToolMandate,
    TerminalToolMandateConfig,
)


def _mandate(
    *,
    nudge_ratio: float = 0.6,
    restrict_ratio: float = 0.75,
    max_time_ms: int = 200,
    terminal_tool: str = "research_finalize",
    required_before: tuple[str, ...] = ("research_list_facts",),
) -> TerminalToolMandate:
    return TerminalToolMandate(
        TerminalToolMandateConfig(
            terminal_tool=terminal_tool,
            required_before=required_before,
            nudge_ratio=nudge_ratio,
            restrict_ratio=restrict_ratio,
        ),
        max_time_ms=max_time_ms,
    )


# ── neutrality ───────────────────────────────────────────────────────────────


def test_disabled_when_no_terminal_tool_configured() -> None:
    """Neutral for the main agent / non-research subagents."""
    mandate = TerminalToolMandate(
        TerminalToolMandateConfig(terminal_tool="", required_before=()),
        max_time_ms=200,
    )
    assert mandate.enabled is False
    time.sleep(0.2)  # well past any ratio
    assert mandate.should_nudge([]) is False
    assert mandate.should_restrict([]) is False


# ── nudge ────────────────────────────────────────────────────────────────────


def test_nudge_fires_after_nudge_ratio_when_terminal_not_called() -> None:
    mandate = _mandate(nudge_ratio=0.1, max_time_ms=200)  # 20ms deadline
    time.sleep(0.05)  # > 20ms
    assert mandate.should_nudge(["research_search", "research_store_fact"]) is True


def test_nudge_does_not_fire_before_nudge_ratio() -> None:
    mandate = _mandate(nudge_ratio=0.9, max_time_ms=100_000)  # 90s deadline
    assert mandate.should_nudge([]) is False


def test_nudge_does_not_fire_if_terminal_already_called() -> None:
    mandate = _mandate(nudge_ratio=0.1, max_time_ms=200)
    time.sleep(0.05)
    assert mandate.should_nudge(["research_finalize"]) is False


def test_nudge_is_idempotent() -> None:
    """Once injected, subsequent calls return False (no repeated notes)."""
    mandate = _mandate(nudge_ratio=0.1, max_time_ms=200)
    time.sleep(0.05)
    assert mandate.should_nudge([]) is True
    assert mandate.should_nudge([]) is False
    assert mandate.nudge_injected is True


# ── restrict ─────────────────────────────────────────────────────────────────


def test_restrict_fires_after_restrict_ratio_when_terminal_not_called() -> None:
    mandate = _mandate(restrict_ratio=0.1, max_time_ms=200)  # 20ms
    time.sleep(0.05)
    assert mandate.should_restrict(["research_search"]) is True


def test_restrict_does_not_fire_before_restrict_ratio() -> None:
    mandate = _mandate(restrict_ratio=0.9, max_time_ms=100_000)
    assert mandate.should_restrict([]) is False


def test_restrict_does_not_fire_if_terminal_already_called() -> None:
    mandate = _mandate(restrict_ratio=0.1, max_time_ms=200)
    time.sleep(0.05)
    assert mandate.should_restrict(["research_list_facts", "research_finalize"]) is False


def test_restrict_is_idempotent() -> None:
    mandate = _mandate(restrict_ratio=0.1, max_time_ms=200)
    time.sleep(0.05)
    assert mandate.should_restrict([]) is True
    assert mandate.should_restrict([]) is False
    assert mandate.restricted is True


# ── escalation ordering ──────────────────────────────────────────────────────


def test_restrict_fires_after_nudge_at_higher_ratio() -> None:
    """At a single point in time past both ratios, both nudge and restrict fire in
    sequence (nudge first, then restrict on the next check)."""
    mandate = _mandate(nudge_ratio=0.1, restrict_ratio=0.1, max_time_ms=200)
    time.sleep(0.05)
    # First call: nudge fires (sets its flag).
    assert mandate.should_nudge([]) is True
    # Now restrict can fire too.
    assert mandate.should_restrict([]) is True
    # Subsequent calls are idempotent.
    assert mandate.should_nudge([]) is False
    assert mandate.should_restrict([]) is False


# ── Iteration-budget awareness (B-047 extension) ─────────────────────────────


def _mandate_with_iterations(
    *,
    nudge_ratio: float = 0.6,
    restrict_ratio: float = 0.75,
    max_iterations: int = 30,
    terminal_tool: str = "research_finalize",
    required_before: tuple[str, ...] = ("research_list_facts",),
) -> TerminalToolMandate:
    """Mandate with iteration tracking enabled (max_time_ms is huge so wall-clock
    never triggers — isolating the iteration-budget path)."""
    return TerminalToolMandate(
        TerminalToolMandateConfig(
            terminal_tool=terminal_tool,
            required_before=required_before,
            nudge_ratio=nudge_ratio,
            restrict_ratio=restrict_ratio,
        ),
        max_time_ms=10_000_000,  # ~2.8h — wall-clock effectively never fires
        max_iterations=max_iterations,
    )


def test_iteration_nudge_fires_before_wall_clock() -> None:
    """When iteration budget is closer to exhaustion than wall-clock, nudge fires
    based on iterations even though wall-clock ratio is near zero.

    Regression: research-agent hit iteration limit (20) at 111s/600s (wall-clock
    ratio 0.186 << nudge_ratio 0.6) → mandate never fired → no finalize.
    """
    mandate = _mandate_with_iterations(max_iterations=30, nudge_ratio=0.6)
    # At iter 18/30 = 0.6 → nudge fires (wall-clock still ~0).
    assert mandate.should_nudge([], iteration=18) is True


def test_iteration_nudge_does_not_fire_below_ratio() -> None:
    """Below nudge_ratio on iterations → no nudge."""
    mandate = _mandate_with_iterations(max_iterations=30, nudge_ratio=0.6)
    assert mandate.should_nudge([], iteration=17) is False


def test_iteration_restrict_fires() -> None:
    """Restrict fires at restrict_ratio based on iterations."""
    mandate = _mandate_with_iterations(max_iterations=30, restrict_ratio=0.75)
    assert mandate.should_restrict([], iteration=23) is True  # 23/30 = 0.767
    assert mandate.should_restrict([], iteration=22) is False  # 22/30 = 0.733


def test_iteration_ratio_uses_max_of_wallclock_and_iterations() -> None:
    """Effective ratio = max(wallclock, iterations). If wall-clock is closer to
    exhaustion, it wins even with iteration tracking enabled."""
    mandate = TerminalToolMandate(
        TerminalToolMandateConfig(
            terminal_tool="t",
            nudge_ratio=0.6,
            restrict_ratio=0.75,
        ),
        max_time_ms=100,  # short → wall-clock fires fast
        max_iterations=1000,  # huge → iterations never fire
    )
    time.sleep(0.07)  # 70ms/100ms = 0.7 > nudge_ratio
    assert mandate.should_nudge([], iteration=1) is True


def test_no_iteration_tracking_falls_back_to_wallclock() -> None:
    """Without max_iterations, mandate is wall-clock-only (back-compat)."""
    mandate = _mandate(max_time_ms=100, nudge_ratio=0.6)
    # No iteration param and no max_iterations → pure wall-clock.
    time.sleep(0.07)
    assert mandate.should_nudge([]) is True


def test_iteration_nudge_idempotent() -> None:
    """Nudge fires once, then stays injected."""
    mandate = _mandate_with_iterations(max_iterations=30, nudge_ratio=0.6)
    assert mandate.should_nudge([], iteration=20) is True
    assert mandate.should_nudge([], iteration=25) is False
