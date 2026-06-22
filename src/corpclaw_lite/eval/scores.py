"""Score containers, weights and recomputation (B-060).

Shared between the deterministic scorer and the LLM judge. The judge emits raw
per-dimension scores; this module recomputes the weighted ``overall`` and the
pass/fail decision deterministically (LLM arithmetic is never trusted — same
approach as the GAIA reference runner.py:606 ``recompute_turn_score``).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Dimension weights (judge_turn.md STEP 4). Must sum to 1.0.
SCORE_WEIGHTS: dict[str, float] = {
    "correctness": 0.25,
    "tool_selection": 0.20,
    "context_retention": 0.20,
    "completeness": 0.15,
    "efficiency": 0.10,
    "personality": 0.05,
    "error_recovery": 0.05,
}

# Pass thresholds (judge_turn.md STEP 4).
PASS_MIN_CORRECTNESS: float = 4.0
PASS_MIN_OVERALL: float = 6.0

DIMENSIONS: tuple[str, ...] = tuple(SCORE_WEIGHTS.keys())


@dataclass
class TurnScore:
    """Per-turn evaluation result (one per scenario turn)."""

    scores: dict[str, float] = field(default_factory=lambda: dict[str, float]())
    overall_score: float = 0.0
    passed: bool = False
    failure_category: str | None = None
    reasoning: str = ""
    # Whether the LLM judge was consulted. False when the deterministic layer
    # settled correctness=0 (pre-check / zero-rule) or exact-match (10).
    judge_used: bool = False
    # Trajectory observability (B-060): populated by EvalRunner._score_turn so
    # the JSON report shows what the agent actually said and did, not just the
    # score. Automatic propagation via asdict() in to_dict().
    final_answer: str = ""
    tools_called: list[str] = field(default_factory=lambda: list[str]())
    transcript: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScenarioScore:
    """Aggregate score across all turns of a scenario."""

    scenario_id: str
    turns: list[TurnScore] = field(default_factory=lambda: list[TurnScore]())
    overall_score: float = 0.0
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "turns": [t.to_dict() for t in self.turns],
            "overall_score": self.overall_score,
            "passed": self.passed,
        }


def clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    """Clamp a dimension score into the valid [0, 10] range."""
    return max(low, min(high, value))


def recompute_overall(scores: dict[str, float]) -> float:
    """Recompute the weighted overall score from per-dimension scores.

    Missing dimensions contribute 0. Each dimension is clamped to [0, 10].
    """
    total = 0.0
    for dim, weight in SCORE_WEIGHTS.items():
        total += clamp(scores.get(dim, 0.0)) * weight
    return round(total, 4)


def decide_pass(scores: dict[str, float], overall: float) -> bool:
    """Apply the GAIA pass/fail decision (judge_turn.md STEP 4):

    1. FAIL if correctness == 0
    2. FAIL if correctness < 4
    3. FAIL if overall < 6.0
    4. PASS otherwise
    """
    correctness = scores.get("correctness", 0.0)
    if correctness <= 0.0:
        return False
    if correctness < PASS_MIN_CORRECTNESS:
        return False
    return overall >= PASS_MIN_OVERALL


def aggregate_scenario(scenario_id: str, turn_scores: list[TurnScore]) -> ScenarioScore:
    """Aggregate per-turn scores into a scenario score.

    A scenario passes only if ALL turns pass; the scenario overall is the mean
    of turn overalls.
    """
    if not turn_scores:
        return ScenarioScore(scenario_id=scenario_id, overall_score=0.0, passed=False)
    overall = round(sum(t.overall_score for t in turn_scores) / len(turn_scores), 4)
    passed = all(t.passed for t in turn_scores)
    return ScenarioScore(
        scenario_id=scenario_id, turns=turn_scores, overall_score=overall, passed=passed
    )


# ───────────────────────── normalization helpers ────────────────────────────

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def extract_numbers(text: str) -> list[float]:
    """Extract all numeric values from ``text`` as floats (commas → dots)."""
    out: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        try:
            out.append(float(match.group().replace(",", ".")))
        except ValueError:
            continue
    return out


def normalize_answer(text: str) -> str:
    """Lowercase, collapse whitespace, strip common punctuation for matching."""
    lowered = text.lower().strip()
    lowered = re.sub(r"[ \t]+", " ", lowered)
    lowered = re.sub(r"\s*\n\s*", " ", lowered)
    # Strip currency symbols and thousands separators around numbers.
    lowered = lowered.replace("₽", "").replace("$", "").replace("€", "")
    return lowered.strip(" .,;:!?\"'()[]")


def numbers_within_tolerance(expected: float, actual: float, tolerance: float = 0.05) -> bool:
    """True if ``actual`` is within ``tolerance`` (5% default) of ``expected``."""
    if expected == 0:
        return abs(actual) <= tolerance
    return abs(actual - expected) / abs(expected) <= tolerance
