"""Tests for SimpleBudgetGuard pause/resume — active time tracking."""

from __future__ import annotations

import time

import pytest

from corpclaw_lite.agent.guards import (
    BudgetExceededError,
    SimpleBudgetGuard,
    SimpleBudgetGuardConfig,
)


class TestPauseResume:
    """Pause/resume excludes waiting time from the budget."""

    def test_pause_excludes_time(self) -> None:
        guard = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_time_ms=200))
        guard.pause()
        time.sleep(0.15)  # 150ms paused — should NOT count
        guard.resume()
        guard.check()  # Should pass: active time ≈ 0ms

    def test_no_pause_counts_all(self) -> None:
        guard = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_time_ms=100))
        time.sleep(0.12)
        with pytest.raises(BudgetExceededError):
            guard.check()

    def test_multiple_pause_resume_cycles(self) -> None:
        guard = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_time_ms=300))
        for _ in range(3):
            guard.pause()
            time.sleep(0.08)  # 80ms paused each = 240ms total paused
            guard.resume()
        time.sleep(0.05)  # 50ms active
        guard.check()  # active ≈ 50ms, well under 300ms

    def test_active_time_triggers_budget(self) -> None:
        guard = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_time_ms=100))
        # Active for 120ms, paused for 200ms (should not matter)
        time.sleep(0.12)
        guard.pause()
        time.sleep(0.2)
        guard.resume()
        with pytest.raises(BudgetExceededError):
            guard.check()

    def test_pause_idempotent(self) -> None:
        guard = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_time_ms=5000))
        guard.pause()
        guard.pause()  # Second pause should be a no-op
        time.sleep(0.05)
        guard.resume()
        # Should only count the single pause interval
        guard.check()

    def test_resume_without_pause_is_noop(self) -> None:
        guard = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_time_ms=5000))
        guard.resume()  # Should not raise or change state
        guard.check()
