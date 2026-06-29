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


class TestIterationBudget:
    """Iteration-budget enforcement via consume_iteration() → check() (B-066).

    The loop calls consume_iteration() then check() at the top of every
    iteration so retry ``continue`` paths cannot burn extra LLM calls past the
    limit. check() uses ``>=``, so reaching the limit trips BEFORE the
    iteration's work.
    """

    def test_check_passes_below_limit(self) -> None:
        guard = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_iterations=3, max_time_ms=60_000))
        guard.consume_iteration()  # iterations_used = 1
        guard.check()  # 1 < 3 → ok
        guard.consume_iteration()  # iterations_used = 2
        guard.check()  # 2 < 3 → ok

    def test_check_raises_at_limit(self) -> None:
        guard = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_iterations=2, max_time_ms=60_000))
        guard.consume_iteration()  # 1
        guard.check()  # 1 < 2 → ok
        guard.consume_iteration()  # 2
        with pytest.raises(BudgetExceededError, match="Iteration budget"):
            guard.check()  # 2 >= 2 → raises

    def test_consume_without_check_does_not_raise(self) -> None:
        """consume_iteration() alone never raises — only check() enforces."""
        guard = SimpleBudgetGuard(SimpleBudgetGuardConfig(max_iterations=1, max_time_ms=60_000))
        guard.consume_iteration()
        guard.consume_iteration()  # over limit, but no raise yet
        guard.consume_iteration()
