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
