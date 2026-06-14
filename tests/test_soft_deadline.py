"""Tests for SoftDeadline wall-clock guard (B-036).

The SoftDeadline measures real elapsed wall-clock time (via time.monotonic), NOT
the active time measured by SimpleBudgetGuard (which pauses during LLM queue
waits per D-040). This is the fix for the subagent-timeout race where
asyncio.wait_for (wall-clock) always fired before the active-time budget guard.
"""

from __future__ import annotations

import time

from corpclaw_lite.agent.guards import (
    SimpleBudgetGuard,
    SimpleBudgetGuardConfig,
    SoftDeadline,
    SoftDeadlineConfig,
)


def test_soft_deadline_not_reached_initially() -> None:
    sd = SoftDeadline(SoftDeadlineConfig(ratio=0.85), max_time_ms=300000)
    assert sd.is_reached() is False
    assert sd.closing_mode is False


def test_soft_deadline_reached_after_ratio_elapsed() -> None:
    # ratio=0.1 * 200ms = 20ms deadline
    sd = SoftDeadline(SoftDeadlineConfig(ratio=0.1), max_time_ms=200)
    time.sleep(0.05)
    assert sd.is_reached() is True


def test_soft_deadline_uses_wall_clock_not_active_time() -> None:
    # Critical race fix: SoftDeadline has no pause()/resume(). Even while a loop
    # is "paused" (waiting in an LLM queue), wall-clock keeps running and the
    # soft deadline still fires — unlike SimpleBudgetGuard which excludes paused
    # time. We prove this by checking SoftDeadline has no pause API and still
    # fires after a sleep that would be "paused" under the budget guard.
    sd = SoftDeadline(SoftDeadlineConfig(ratio=0.5), max_time_ms=100)  # 50ms deadline
    assert not hasattr(sd, "pause")
    assert not hasattr(sd, "resume")
    time.sleep(0.06)
    assert sd.is_reached() is True


def test_budget_guard_excludes_pause_but_soft_deadline_does_not() -> None:
    # Contrast: SimpleBudgetGuard with a pause interval measures less elapsed
    # time than SoftDeadline over the same wall-clock span.
    budget = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_time_ms=1000))
    budget.pause()
    time.sleep(0.05)
    budget.resume()
    # active time near zero (paused interval excluded) -> well under 1000ms
    assert budget._active_ms() < 50

    sd = SoftDeadline(SoftDeadlineConfig(ratio=0.01), max_time_ms=1000)  # 10ms deadline
    time.sleep(0.02)
    assert sd.is_reached() is True


def test_enter_closing_mode() -> None:
    sd = SoftDeadline(SoftDeadlineConfig(ratio=0.1), max_time_ms=10000)
    assert sd.closing_mode is False
    sd.enter_closing_mode()
    assert sd.closing_mode is True


def test_disabled_never_reached() -> None:
    sd = SoftDeadline(SoftDeadlineConfig(enabled=False), max_time_ms=100)
    time.sleep(0.02)
    assert sd.is_reached() is False


def test_soft_deadline_ratio_setting_default() -> None:
    # AgentSettings exposes soft_deadline_ratio (wired into loop.run()).
    from corpclaw_lite.config.settings import AgentSettings

    settings = AgentSettings()
    assert settings.soft_deadline_ratio == 0.85
