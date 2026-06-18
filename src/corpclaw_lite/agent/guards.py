"""Simplified guards for simple agent execution mode."""

from __future__ import annotations

import hashlib
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

    last_tool_error_signature: tuple[tuple[str, str], ...] | None = None
    same_error_count: int = 0


class SimpleProgressGuard:
    """Detects loops based on repeated tool errors.

    When the same tool action produces the same error across multiple model
    turns, the caller can give the model a recovery hint before hard-stopping.
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

    def detect_loop_for_results(self, tool_results: list[tuple[str, str]]) -> bool:
        """Check if one model action repeated the same failing strategy."""

        if not self.config.enabled:
            return False

        signatures: list[tuple[str, str]] = []
        for tool_name, result in tool_results:
            if not result.startswith(TOOL_ERROR_PREFIX):
                # Any successful result in the action is progress.
                self.state.last_tool_error_signature = None
                self.state.same_error_count = 0
                return False

            normalized_error = self._normalize_error(result)
            signature = (tool_name, normalized_error)
            if signature not in signatures:
                signatures.append(signature)

        if not signatures:
            self.state.last_tool_error_signature = None
            self.state.same_error_count = 0
            return False

        action_signature = tuple(signatures)

        if action_signature == self.state.last_tool_error_signature:
            self.state.same_error_count += 1
        else:
            self.state.last_tool_error_signature = action_signature
            self.state.same_error_count = 1

        return self.state.same_error_count >= self.config.max_same_tool_error

    def detect_loop(self, tool_name: str, result: str) -> bool:
        """Check if a single tool execution repeated the same error."""
        return self.detect_loop_for_results([(tool_name, result)])

    def reset(self) -> None:
        """Reset guard state for new conversation."""
        self.state = SimpleProgressGuardState()


@dataclass
class ResultDedupGuardConfig:
    """Configuration for the result-based dedup guard (B-055).

    Complementary to :class:`SimpleProgressGuard`: the progress guard detects
    repeated *errors* (normalized by ``_normalize_error``), while this guard
    detects repeated *successful results* — the case where a local LLM keeps
    calling the same tool with the same arguments and getting the same answer
    back. Prompt-only "do not loop" instructions are ignored under sampling
    pressure (confirmed by the GAIA eval taxonomy), so a deterministic
    result-hash counter is the reliable signal.
    """

    enabled: bool = True
    # How many identical (by hash) tool results in a row count as a loop.
    max_repeats: int = 2


class ResultDedupGuard:
    """Detects loops where a tool returns the same successful result repeatedly.

    The dedup key is the SHA-256 hash of the result string (truncated to 12
    chars, matching ``_payload_hash`` in loop.py), **not** the arguments. This
    is the key insight from the GAIA reference: identical arguments do not
    imply a loop (the underlying file may have changed between calls), but an
    identical result does. The caller injects a recovery hint and lets the
    model try again with that context.
    """

    def __init__(self, config: ResultDedupGuardConfig | None = None) -> None:
        self.config = config or ResultDedupGuardConfig()
        self._seen: dict[str, int] = {}

    def _result_hash(self, result: str) -> str:
        return hashlib.sha256(result.encode("utf-8")).hexdigest()[:12]

    def detect(self, tool_name: str, result: str) -> bool:
        """Record ``result`` and return True if it has now been seen ``max_repeats`` times.

        ``tool_name`` is accepted for symmetry with :meth:`SimpleProgressGuard.detect_loop`
        and for trace logging; the dedup key is the result hash only.
        """
        _ = tool_name  # accepted for API symmetry, not used in the key
        if not self.config.enabled:
            return False
        key = self._result_hash(result)
        self._seen[key] = self._seen.get(key, 0) + 1
        return self._seen[key] >= self.config.max_repeats

    def last_count(self, result: str) -> int:
        """Return how many times ``result`` has been seen (for trace fields)."""
        return self._seen.get(self._result_hash(result), 0)

    def reset(self) -> None:
        """Reset guard state for new conversation."""
        self._seen.clear()


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


@dataclass
class SoftDeadlineConfig:
    """Configuration for the wall-clock soft deadline (closing mode trigger)."""

    enabled: bool = True
    ratio: float = 0.85  # fraction of max_time_ms at which closing mode starts


class SoftDeadline:
    """Wall-clock soft deadline that triggers agent closing mode.

    Unlike :class:`SimpleBudgetGuard`, which measures *active* time (pausing
    during LLM queue waits per D-040), ``SoftDeadline`` measures real elapsed
    wall-clock time via :func:`time.monotonic`. This is the fix for the subagent
    timeout race: ``asyncio.wait_for`` in ``SubagentDispatcher.dispatch`` also
    uses wall-clock and would otherwise always fire before the active-time
    budget guard, so the D-038 final-answer rescue never ran for subagents.
    """

    def __init__(
        self,
        config: SoftDeadlineConfig | None = None,
        *,
        max_time_ms: int = 300000,
    ) -> None:
        self.config = config or SoftDeadlineConfig()
        self._max_time_ms = max_time_ms
        self._start = time.monotonic()
        self._closing_mode = False

    def is_reached(self) -> bool:
        if not self.config.enabled:
            return False
        elapsed_ms = (time.monotonic() - self._start) * 1000
        return elapsed_ms >= self._deadline_ms()

    def _deadline_ms(self) -> float:
        if not self.config.enabled:
            return float("inf")
        return self.config.ratio * self._max_time_ms

    @property
    def closing_mode(self) -> bool:
        return self._closing_mode

    def enter_closing_mode(self) -> None:
        self._closing_mode = True


@dataclass
class TerminalToolMandateConfig:
    """Configuration for the workflow-finalize guard (B-047).

    Knows that a research subagent has a mandatory terminal tool (research_finalize)
    that must be reached before the budget runs out. The guard escalates in two
    deterministic steps — a soft nudge and a hard tool-schema restriction — so a local
    LLM that keeps gathering data instead of synthesizing is pushed toward finalization
    *before* the wall-clock limit cancels the run.

    This is deliberately NOT LLM-based planning: there is no objective storage, no
    verifier, no re-planner. It is a static declaration of the required terminal tool
    plus wall-clock thresholds, evaluated from recorded tool-call evidence.
    """

    enabled: bool = True
    terminal_tool: str = ""
    required_before: tuple[str, ...] = ()
    # Fraction of max_wall_time_ms at which the soft nudge (system note) is injected.
    nudge_ratio: float = 0.6
    # Fraction at which tools_schema is restricted to {required_before + terminal_tool}.
    restrict_ratio: float = 0.75


class TerminalToolMandate:
    """Workflow-finalize guard: push the run toward its mandatory terminal tool.

    Tracks elapsed wall-clock time (via :func:`time.monotonic`, mirroring
    :class:`SoftDeadline`) and, when the run is running low on budget AND has not yet
    called ``terminal_tool``, signals the caller to escalate:

    - :meth:`should_nudge`: inject a system note telling the model to stop gathering
      and call ``research_list_facts`` then ``research_finalize``.
    - :meth:`should_restrict`: narrow the tool schema to ``required_before`` plus the
      terminal tool, so the model can only finalize.

    Neutral (``enabled=False``) when no terminal tool is configured — the main agent
    and non-research subagents are unaffected.
    """

    def __init__(
        self,
        config: TerminalToolMandateConfig | None = None,
        *,
        max_time_ms: int = 300000,
    ) -> None:
        self.config = config or TerminalToolMandateConfig()
        self._max_time_ms = max_time_ms
        self._start = time.monotonic()
        self._nudge_injected = False
        self._restricted = False

    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.config.terminal_tool)

    def _elapsed_ratio(self) -> float:
        elapsed_ms = (time.monotonic() - self._start) * 1000
        return elapsed_ms / self._max_time_ms if self._max_time_ms > 0 else 1.0

    def elapsed_ratio(self) -> float:
        """Current fraction of max_wall_time_ms elapsed (for telemetry)."""
        return self._elapsed_ratio()

    def terminal_called(self, tools_used: list[str]) -> bool:
        """Whether the mandatory terminal tool has been called during this run."""
        return self.config.terminal_tool in tools_used

    def should_nudge(self, tools_used: list[str]) -> bool:
        if not self.enabled or self._nudge_injected:
            return False
        if self.terminal_called(tools_used):
            return False
        if self._elapsed_ratio() < self.config.nudge_ratio:
            return False
        self._nudge_injected = True
        return True

    def should_restrict(self, tools_used: list[str]) -> bool:
        if not self.enabled or self._restricted:
            return False
        if self.terminal_called(tools_used):
            return False
        if self._elapsed_ratio() < self.config.restrict_ratio:
            return False
        self._restricted = True
        return True

    @property
    def nudge_injected(self) -> bool:
        return self._nudge_injected

    @property
    def restricted(self) -> bool:
        return self._restricted


__all__ = [
    "BudgetExceededError",
    "ResultDedupGuard",
    "ResultDedupGuardConfig",
    "SimpleBudgetGuard",
    "SimpleBudgetGuardConfig",
    "SimpleProgressGuard",
    "SimpleProgressGuardConfig",
    "SoftDeadline",
    "SoftDeadlineConfig",
    "TerminalToolMandate",
    "TerminalToolMandateConfig",
]
