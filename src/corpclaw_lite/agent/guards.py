"""Simplified guards for simple agent execution mode."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from corpclaw_lite.extensions.tools.base import TOOL_ERROR_PREFIX


class BudgetExceededError(Exception):
    """Raised when budget limits are exceeded."""

    pass


@dataclass
class SimpleProgressGuardConfig:
    """Configuration for simple progress guard."""

    enabled: bool = True
    max_same_tool_error: int = 3  # Max repeats of same tool error before warning


@dataclass
class SimpleProgressGuardState:
    """Mutable state for progress guard."""

    last_tool_error_signature: tuple[str, str] | None = None
    same_error_count: int = 0


class SimpleProgressGuard:
    """Detects loops based on repeated tool errors.

    When the same tool produces the same error multiple times, injects a warning
    into the conversation to help the model break out of the loop.
    """

    def __init__(self, config: SimpleProgressGuardConfig | None = None) -> None:
        self.config = config or SimpleProgressGuardConfig()
        self.state = SimpleProgressGuardState()

    def _normalize_error(self, error: str) -> str:
        """Normalize error message for comparison."""
        # Remove variable parts like timestamps, file paths, etc.
        normalized = re.sub(r"\b\d+\b", "N", error)
        normalized = re.sub(r"\s+", " ", normalized).strip().lower()
        return normalized[:200]  # Truncate for comparison stability

    def detect_loop(self, tool_name: str, result: str) -> bool:
        """Check if tool execution resulted in repeated error.

        Args:
            tool_name: Name of the executed tool.
            result: Tool execution result.

        Returns:
            True if loop detected (same error repeated), False otherwise.
        """
        if not self.config.enabled:
            return False

        if not result.startswith(TOOL_ERROR_PREFIX):
            # Reset on success
            self.state.last_tool_error_signature = None
            self.state.same_error_count = 0
            return False

        normalized_error = self._normalize_error(result)
        signature = (tool_name, normalized_error)

        if signature == self.state.last_tool_error_signature:
            self.state.same_error_count += 1
        else:
            self.state.last_tool_error_signature = signature
            self.state.same_error_count = 1

        return self.state.same_error_count >= self.config.max_same_tool_error

    def reset(self) -> None:
        """Reset guard state for new conversation."""
        self.state = SimpleProgressGuardState()


@dataclass
class SimpleBudgetGuardConfig:
    """Configuration for simple budget guard."""

    enabled: bool = True
    max_iterations: int = 15
    max_tool_calls: int = 30
    max_time_ms: int = 300000  # 5 minutes by default


@dataclass
class SimpleBudgetGuardState:
    """Mutable state for budget guard."""

    started_at_monotonic: float = field(default_factory=time.monotonic)
    iterations_used: int = 0
    tool_calls_used: int = 0
    paused_at: float | None = None
    total_paused_ms: float = 0.0


class SimpleBudgetGuard:
    """Tracks resource consumption and raises BudgetExceededError when limits exceeded.

    The time budget measures *active* processing time — call ``pause()`` before
    waiting for an external resource (e.g., an LLM queue slot) and ``resume()``
    when processing resumes. Paused time does not count toward the limit.
    """

    def __init__(self, config: SimpleBudgetGuardConfig | None = None) -> None:
        self.config = config or SimpleBudgetGuardConfig()
        self.state = SimpleBudgetGuardState()

    def consume_iteration(self) -> None:
        """Record iteration consumption."""
        self.state.iterations_used += 1

    def consume_tool_calls(self, count: int) -> None:
        """Record tool call consumption."""
        if count > 0:
            self.state.tool_calls_used += count

    def pause(self) -> None:
        """Pause the time budget (e.g., while waiting in an LLM queue)."""
        if self.state.paused_at is None:
            self.state.paused_at = time.monotonic()

    def resume(self) -> None:
        """Resume the time budget after a ``pause()``."""
        if self.state.paused_at is not None:
            self.state.total_paused_ms += (time.monotonic() - self.state.paused_at) * 1000
            self.state.paused_at = None

    def _active_ms(self) -> float:
        """Return elapsed active milliseconds (wall clock minus paused time)."""
        elapsed_ms = (time.monotonic() - self.state.started_at_monotonic) * 1000
        paused_ms = self.state.total_paused_ms
        if self.state.paused_at is not None:
            paused_ms += (time.monotonic() - self.state.paused_at) * 1000
        return elapsed_ms - paused_ms

    def check(self) -> None:
        """Check if budget limits are exceeded.

        Raises:
            BudgetExceededError: When any budget limit is exceeded.
        """
        if not self.config.enabled:
            return

        if self.state.iterations_used >= self.config.max_iterations:
            raise BudgetExceededError(
                f"Iteration budget exceeded: "
                f"{self.state.iterations_used}/{self.config.max_iterations}"
            )

        if self.state.tool_calls_used >= self.config.max_tool_calls:
            raise BudgetExceededError(
                f"Tool call budget exceeded: "
                f"{self.state.tool_calls_used}/{self.config.max_tool_calls}"
            )

        active_ms = self._active_ms()
        if active_ms >= self.config.max_time_ms:
            raise BudgetExceededError(
                f"Time budget exceeded: {int(active_ms)}ms/{self.config.max_time_ms}ms"
            )

    def reset(self) -> None:
        """Reset guard state for new conversation."""
        self.state = SimpleBudgetGuardState()


__all__ = [
    "BudgetExceededError",
    "SimpleBudgetGuard",
    "SimpleBudgetGuardConfig",
    "SimpleProgressGuard",
    "SimpleProgressGuardConfig",
]
