"""Calibration scorer — compare expected vs actual agent behaviour."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from corpclaw_lite.calibration.scenarios import CalibrationScenario
from corpclaw_lite.calibration.trajectory import Trajectory

__all__ = [
    "CalibrationScore",
    "CalibrationScorer",
    "ScenarioResult",
]


@dataclass
class ScenarioResult:
    """Result of running and scoring one scenario."""

    scenario: CalibrationScenario
    trajectory: Trajectory
    passed: bool
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON logging and cloud model analysis."""
        return {
            "scenario_id": self.scenario.id,
            "category": self.scenario.category,
            "user_message": self.scenario.user_message,
            "passed": self.passed,
            "failure_reason": self.failure_reason,
            "expected_tools": self.scenario.expected.tool_calls,
            "actual_tools": self.trajectory.tool_calls_sequence(),
            "final_answer_preview": self.trajectory.final_answer[:300],
            "status": self.trajectory.status,
        }


@dataclass
class CalibrationScore:
    """Aggregate score across all scenarios."""

    passed: int
    total: int
    by_category: dict[str, tuple[int, int]]  # category -> (passed, total)

    @property
    def pct(self) -> float:
        """Percentage of passed scenarios."""
        return (self.passed / self.total * 100) if self.total > 0 else 0.0


class CalibrationScorer:
    """Compare actual trajectory against expected outcome.

    Scoring rules:
    - tool_calls: expected tools must appear as a subsequence of actual tools
      (extras are allowed — agent may do additional lookups)
    - must_read: specific file must appear in read_file arguments
    - contains: substring must appear in final answer (case-insensitive)
    - has_content: final answer must be non-empty
    """

    def score(
        self,
        scenario: CalibrationScenario,
        trajectory: Trajectory,
    ) -> ScenarioResult:
        """Score a single scenario execution."""
        expected = scenario.expected
        actual_tools = trajectory.tool_calls_sequence()

        # Check 1: Were the expected tools called (as a subsequence)?
        if expected.tool_calls:
            if not self._is_subsequence(expected.tool_calls, actual_tools):
                return ScenarioResult(
                    scenario=scenario,
                    trajectory=trajectory,
                    passed=False,
                    failure_reason=(
                        f"Expected tools {expected.tool_calls} as subsequence "
                        f"of actual {actual_tools}"
                    ),
                )
        elif actual_tools and scenario.category == "no_tool":
            # Expected no tools but tools were called in no_tool category
            return ScenarioResult(
                scenario=scenario,
                trajectory=trajectory,
                passed=False,
                failure_reason=f"Expected no tool calls, but agent called {actual_tools}",
            )

        # Check 2: Was the expected file read?
        if expected.must_read:
            read_file_args = [
                s.tool_args
                for s in trajectory.steps
                if s.step_type == "tool_call" and s.tool_name == "read_file" and s.tool_args
            ]
            found = any(expected.must_read in str(args.get("path", "")) for args in read_file_args)
            if not found:
                return ScenarioResult(
                    scenario=scenario,
                    trajectory=trajectory,
                    passed=False,
                    failure_reason=(
                        f"Expected read of '{expected.must_read}' not found in tool args"
                    ),
                )

        # Check 3: Does the answer contain expected text?
        if expected.contains and expected.contains.lower() not in trajectory.final_answer.lower():
            return ScenarioResult(
                scenario=scenario,
                trajectory=trajectory,
                passed=False,
                failure_reason=(
                    f"Expected '{expected.contains}' in answer, "
                    f"got: '{trajectory.final_answer[:200]}'"
                ),
            )

        # Check 4: Non-empty answer?
        if expected.has_content and not trajectory.final_answer.strip():
            return ScenarioResult(
                scenario=scenario,
                trajectory=trajectory,
                passed=False,
                failure_reason="Expected non-empty answer, got empty",
            )

        # Check 5: Agent didn't error out
        if trajectory.status not in ("ok", "loop"):
            return ScenarioResult(
                scenario=scenario,
                trajectory=trajectory,
                passed=False,
                failure_reason=f"Agent finished with error status: {trajectory.status}",
            )

        return ScenarioResult(
            scenario=scenario,
            trajectory=trajectory,
            passed=True,
        )

    def score_all(self, results: list[ScenarioResult]) -> CalibrationScore:
        """Calculate aggregate score across all results."""
        passed = sum(1 for r in results if r.passed)
        total = len(results)

        by_category: dict[str, tuple[int, int]] = {}
        for r in results:
            cat = r.scenario.category
            cat_passed, cat_total = by_category.get(cat, (0, 0))
            by_category[cat] = (cat_passed + (1 if r.passed else 0), cat_total + 1)

        return CalibrationScore(passed=passed, total=total, by_category=by_category)

    @staticmethod
    def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
        """Check if expected is a subsequence of actual (order matters, gaps allowed)."""
        it = iter(actual)
        return all(tool in it for tool in expected)
