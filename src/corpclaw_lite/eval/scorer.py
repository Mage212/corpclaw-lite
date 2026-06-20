"""Deterministic scoring layer (B-060, step 3).

Implements the pre-check and automatic-zero rules from judge_turn.md STEP 1-2,
plus exact-match for clean ground truth. When this layer cannot settle the
correctness score (i.e. the answer is plausibly correct but not an exact match,
and no zero-rule fired), it returns ``judge_needed=True`` so the LLM judge can
score the remaining dimensions.

This layer is always run before the LLM judge: it is the cheap, deterministic
backstop that prevents the judge from being asked to score obviously-failed or
obviously-correct answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from corpclaw_lite.eval.scenarios import ScenarioTurn
from corpclaw_lite.eval.scores import (
    TurnScore,
    extract_numbers,
    normalize_answer,
    numbers_within_tolerance,
)

__all__ = ["DeterministicScorer", "DeterministicResult"]


# Pre-check: a response that is mostly brackets/braces with <3 readable words.
_GARBLED_RE = re.compile(r"^[\s{}\[\]\"':,0-9.]*$")
# Raw JSON leak: a response that looks like a JSON object with tool-ish keys.
_JSON_LEAK_RE = re.compile(r'^\s*\{.*"(chunks|scores|tool|result|content)"', re.DOTALL)
# Tool-call artifact: response is only a [tool:X] label.
_TOOL_ARTIFACT_RE = re.compile(r"^\s*\[tool:[a-zA-Z_]+\]\s*$")
# Lazy refusal phrases (EN + RU). Zero-rule only when a query tool was NOT called.
_LAZY_REFUSAL_PHRASES = (
    "can't find",
    "cannot find",
    "could not find",
    "unable to find",
    "не могу найти",
    "не удалось найти",
    "не нашёл",
    "не нашел",
)


@dataclass
class DeterministicResult:
    """Outcome of the deterministic scoring layer for one turn.

    ``score`` is populated with a settled ``TurnScore`` when the layer resolved
    correctness decisively (judge_used=False). When ``judge_needed`` is True the
    caller must invoke the LLM judge; ``score`` then carries only the
    zero-rule-derived constraints the judge must respect.
    """

    score: TurnScore
    judge_needed: bool


class DeterministicScorer:
    """Apply judge_turn.md pre-check and zero-rules deterministically."""

    def score_turn(
        self,
        turn: ScenarioTurn,
        final_answer: str,
        tools_called: list[str],
    ) -> DeterministicResult:
        answer = (final_answer or "").strip()

        # STEP 1 — pre-check.
        precheck = self._precheck(answer)
        if precheck is not None:
            return DeterministicResult(
                score=self._settled_zero(precheck),
                judge_needed=False,
            )

        expected = turn.expected_answer

        # Null-answer branch: ground truth asserts NO answer exists.
        if expected is None:
            return self._score_null_answer(answer, tools_called)

        # Non-null ground truth: zero-rules + exact-match.
        return self._score_against_ground_truth(answer, expected, tools_called, turn)

    # ─────────────────────────── pre-check ────────────────────────────────

    def _precheck(self, answer: str) -> str | None:
        """Return a failure_category if the answer fails STEP 1, else None."""
        if not answer:
            return "wrong_answer"
        if _TOOL_ARTIFACT_RE.match(answer):
            return "garbled_output"
        if _JSON_LEAK_RE.match(answer):
            return "garbled_output"
        # A bare numeric answer (e.g. "3650", "48") is a VALID response to a
        # numeric question — never treat it as garbled. Only flag truly garbled
        # output: mostly brackets/braces with <3 readable words AND no digits.
        has_digit = any(ch.isdigit() for ch in answer)
        if has_digit:
            return None
        words = list(re.findall(r"[A-Za-zА-Яа-яёЁ]{2,}", answer))
        if len(words) < 3 and _GARBLED_RE.match(answer):
            return "garbled_output"
        return None

    # ─────────────────────────── null answer ─────────────────────────────

    def _score_null_answer(self, answer: str, tools_called: list[str]) -> DeterministicResult:
        lowered = answer.lower()
        # Did the agent invent a specific number when none exists?
        if extract_numbers(answer):
            # Inventing a specific figure for a null-answer turn = correctness 0.
            return DeterministicResult(
                score=self._settled_zero("hallucinated_source"),
                judge_needed=False,
            )
        # Said "I don't know" / "not in the document" — correct behaviour.
        dont_know = any(
            p in lowered
            for p in (
                "don't know",
                "do not know",
                "not in the document",
                "not mentioned",
                "no information",
                "не знаю",
                "не указан",
                "не упом",
                "нет данных",
                "в документе нет",
                "документ не",
            )
        )
        if dont_know:
            # The agent did the right thing; let the judge score the remaining
            # dims, but correctness is effectively 10. We settle without judge
            # for the common clean case.
            scores: dict[str, float] = {
                "correctness": 10.0,
                "tool_selection": 8.0,
                "completeness": 8.0,
                "context_retention": 8.0,
                "efficiency": 8.0,
                "personality": 8.0,
                "error_recovery": 8.0,
            }
            score = TurnScore(
                scores=scores,
                reasoning="Adversarial null-answer handled: agent correctly stated "
                "the information is unavailable.",
            )
            score.overall_score = self._overall(score.scores)
            score.passed = self._pass(score.scores, score.overall_score)
            return DeterministicResult(score=score, judge_needed=False)
        # Ambiguous: neither a clear "don't know" nor an invented number. The
        # judge must decide whether this is a hedge (ok) or an evasion (fail).
        return DeterministicResult(
            score=TurnScore(reasoning="Null-answer turn; judge must classify response."),
            judge_needed=True,
        )

    # ────────────────────────── ground truth ─────────────────────────────

    def _score_against_ground_truth(
        self,
        answer: str,
        expected: str,
        tools_called: list[str],
        turn: ScenarioTurn,
    ) -> DeterministicResult:
        norm_ans = normalize_answer(answer)
        norm_exp = normalize_answer(expected)

        # Exact (normalized) match → correctness 10, settle without judge.
        if norm_exp and norm_exp in norm_ans:
            return DeterministicResult(
                score=self._settled_correct(reasoning="Exact normalized match."),
                judge_needed=False,
            )

        # Zero-rule: lazy refusal without a query tool call. Checked BEFORE the
        # number rule because a refusal explains the absence of a number — it is
        # not a "wrong number".
        lowered = answer.lower()
        if any(p in lowered for p in _LAZY_REFUSAL_PHRASES):
            query_tools = {"read_file", "table_query", "list_files", "excel_workbook"}
            if not any(t in query_tools for t in tools_called):
                return DeterministicResult(
                    score=self._settled_zero("lazy_refusal"),
                    judge_needed=False,
                )
            # Refusal WITH a query-tool call → the judge must decide whether the
            # tool genuinely returned nothing (acceptable) or the agent gave up.
            return DeterministicResult(
                score=TurnScore(reasoning="Lazy refusal after a query-tool call; judge required."),
                judge_needed=True,
            )

        # Zero-rule: wrong number. Only fires when the answer CONTAINS a number
        # that is >5% off the ground truth. An answer with no digits (a textual
        # answer, or a hedge) is not a "wrong number" — the judge must score it.
        exp_numbers = extract_numbers(expected)
        if exp_numbers:
            ans_numbers = extract_numbers(answer)
            if ans_numbers and self._wrong_number(exp_numbers, ans_numbers):
                return DeterministicResult(
                    score=self._settled_zero("wrong_number"),
                    judge_needed=False,
                )

        # Optional must_contain substring check (deterministic).
        if turn.must_contain and turn.must_contain.lower() not in lowered:
            return DeterministicResult(
                score=self._settled_zero("missing_required_content"),
                judge_needed=False,
            )

        # No zero-rule fired and not an exact match → judge must score.
        return DeterministicResult(
            score=TurnScore(reasoning="No deterministic verdict; judge required."),
            judge_needed=True,
        )

    def _wrong_number(self, expected: list[float], actual: list[float]) -> bool:
        """True if any expected number has no close match among actual numbers.

        Caller guarantees ``actual`` is non-empty: a number-free answer is not a
        "wrong number" (it is either a refusal or a textual answer for the
        judge).
        """
        if not expected or not actual:
            return False
        for exp in expected:
            if not any(numbers_within_tolerance(exp, act) for act in actual):
                return True
        return False

    # ──────────────────────────── helpers ────────────────────────────────

    def _settled_zero(self, category: str) -> TurnScore:
        scores: dict[str, float] = dict.fromkeys(
            (
                "correctness",
                "tool_selection",
                "context_retention",
                "completeness",
                "efficiency",
                "personality",
                "error_recovery",
            ),
            0.0,
        )
        return TurnScore(
            scores=scores,
            overall_score=0.0,
            passed=False,
            failure_category=category,
            reasoning=f"Deterministic zero-rule: {category}",
        )

    def _settled_correct(self, reasoning: str) -> TurnScore:
        scores: dict[str, float] = {
            "correctness": 10.0,
            "tool_selection": 8.0,
            "completeness": 8.0,
            "context_retention": 8.0,
            "efficiency": 8.0,
            "personality": 8.0,
            "error_recovery": 8.0,
        }
        overall = self._overall(scores)
        return TurnScore(
            scores=scores,
            overall_score=overall,
            passed=self._pass(scores, overall),
            reasoning=reasoning,
        )

    def _overall(self, scores: dict[str, float]) -> float:
        from corpclaw_lite.eval.scores import recompute_overall

        return recompute_overall(scores)

    def _pass(self, scores: dict[str, float], overall: float) -> bool:
        from corpclaw_lite.eval.scores import decide_pass

        return decide_pass(scores, overall)
