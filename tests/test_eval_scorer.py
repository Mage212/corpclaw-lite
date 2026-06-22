"""Tests for the deterministic scoring layer (B-060, step 3)."""

from __future__ import annotations

import pytest

from corpclaw_lite.calibration.trajectory import Trajectory, TrajectoryStep
from corpclaw_lite.eval.scenarios import ScenarioTurn
from corpclaw_lite.eval.scorer import DeterministicScorer

scorer = DeterministicScorer()


def _traj(tools: list[str] | None = None, dispatch_ids: list[str] | None = None) -> Trajectory:
    """Build a minimal Trajectory from a flat tool-call sequence.

    ``dispatch_ids`` adds dispatch_subagent calls with the given subagent_id
    args (needed for expected_subagent checks).
    """
    steps: list[TrajectoryStep] = [
        TrajectoryStep(step_type="tool_call", tool_name=t) for t in tools or []
    ]
    steps.extend(
        TrajectoryStep(
            step_type="tool_call",
            tool_name="dispatch_subagent",
            tool_args={"subagent_id": sid, "task": "do it"},
        )
        for sid in dispatch_ids or []
    )
    return Trajectory(scenario_id="t", steps=steps)


def _turn(
    expected: str | None = "sentinel",
    must_contain: str | None = None,
) -> ScenarioTurn:
    return ScenarioTurn(
        user_message="x",
        expected_answer=None if expected == "sentinel" else expected,
        must_contain=must_contain,
    )


# ────────────────────────────── pre-check ──────────────────────────────────


def test_empty_answer_fails_wrong_answer() -> None:
    res = scorer.score_turn(_turn("42"), "", trajectory=_traj([]))
    assert not res.judge_needed
    assert res.score.failure_category == "wrong_answer"
    assert res.score.scores["correctness"] == 0.0
    assert not res.score.passed


def test_tool_artifact_fails_garbled() -> None:
    res = scorer.score_turn(_turn("42"), "[tool:query_files]", trajectory=_traj([]))
    assert not res.judge_needed
    assert res.score.failure_category == "garbled_output"
    assert res.score.scores["correctness"] == 0.0


def test_json_leak_fails_garbled() -> None:
    res = scorer.score_turn(_turn("42"), '{"chunks": [], "result": "x"}', trajectory=_traj([]))
    assert not res.judge_needed
    assert res.score.failure_category == "garbled_output"


# ──────────────────────────── null-answer branch ───────────────────────────


def test_null_answer_invention_needs_judge() -> None:
    """Regression: a number without an explicit refusal phrase is no longer a
    deterministic zero. It is a *potential* hallucination, but the scorer cannot
    reliably distinguish an invented figure from a factual aside, so the judge
    must classify it. (Previously this was a hard hallucinated_source zero,
    which caused false positives — see test_null_answer_context_number_passes.)"""
    res = scorer.score_turn(
        _turn(), "За стаж 10 лет положено 5 дней.", trajectory=_traj(["read_file"])
    )
    assert res.judge_needed
    assert res.score.failure_category == "hallucinated_source"


def test_null_answer_context_number_passes() -> None:
    """Regression (live A/B on Qwen/gemma): the agent correctly stated the
    information is unavailable while quoting a number from the source as
    context — "В документе policy.txt нет информации о доп. отпуске за стаж.
    Указаны только базовые 28 дней." Previously any number triggered a hard
    hallucinated_source zero. Now the refusal is detected first and the context
    number is allowed."""
    res = scorer.score_turn(
        _turn(),
        "В документе policy.txt нет информации о дополнительном отпуске за стаж. "
        "Указаны только базовые 28 дней.",
        trajectory=_traj(["read_file"]),
    )
    assert not res.judge_needed
    assert res.score.failure_category is None
    assert res.score.scores["correctness"] == 10.0
    assert res.score.passed


def test_null_answer_dont_know_passes() -> None:
    res = scorer.score_turn(
        _turn(), "В документе нет информации об этом.", trajectory=_traj(["read_file"])
    )
    assert not res.judge_needed
    assert res.score.failure_category is None
    assert res.score.scores["correctness"] == 10.0
    assert res.score.passed


def test_null_answer_dont_know_english() -> None:
    res = scorer.score_turn(
        _turn(), "I don't know — it's not in the document.", trajectory=_traj([])
    )
    assert not res.judge_needed
    assert res.score.scores["correctness"] == 10.0


def test_null_answer_extended_refusal_phrases() -> None:
    """Newly added refusal phrases: 'нет информации', 'не предусмотр',
    'не содержит'. Each must settle as a correct refusal even when a number
    is present in the answer."""
    cases = [
        "Нет информации о доплатах в этом документе.",
        "Доплата за стаж не предусмотрена.",
        "Документ не содержит упоминания о премии.",
    ]
    for answer in cases:
        res = scorer.score_turn(_turn(), answer, trajectory=_traj(["read_file"]))
        assert not res.judge_needed, f"Expected settled pass for: {answer!r}"
        assert res.score.scores["correctness"] == 10.0
        assert res.score.passed


def test_null_answer_ambiguous_needs_judge() -> None:
    """A hedge that's neither 'don't know' nor an invented number → judge."""
    res = scorer.score_turn(
        _turn(), "This depends on several factors we'd need to verify.", trajectory=_traj([])
    )
    assert res.judge_needed


def test_behavioral_null_defers_to_judge_not_hallucinated() -> None:
    """Regression: a turn with expected_tools but NO expected_answer is a
    behavioural scenario (grade the tool path, not the answer). It must NOT
    fall into the adversarial null-answer branch (which would flag any number
    in the response as 'hallucinated_source'). The judge must score it."""
    turn = ScenarioTurn(
        user_message="Create a file",
        expected_answer=None,
        expected_tools=["write_file"],
    )
    res = scorer.score_turn(
        turn, "Created todo.txt with the requested content.", trajectory=_traj(["write_file"])
    )
    assert res.judge_needed
    assert res.score.failure_category is None  # not hallucinated_source


# ─────────────────────────── ground-truth branch ───────────────────────────


def test_exact_match_settles_correct() -> None:
    res = scorer.score_turn(
        _turn("3650"), "Общий доход: 3650 рублей.", trajectory=_traj(["table_query"])
    )
    assert not res.judge_needed
    assert res.score.scores["correctness"] == 10.0
    assert res.score.passed


def test_exact_match_case_insensitive_and_normalized() -> None:
    res = scorer.score_turn(_turn("South"), " SOUTH ", trajectory=_traj(["table_query"]))
    assert not res.judge_needed
    assert res.score.scores["correctness"] == 10.0


def test_wrong_number_fails() -> None:
    """Ground truth 3650, answer 3000 (>5% off) → correctness 0."""
    res = scorer.score_turn(_turn("3650"), "Общий доход: 3000.", trajectory=_traj(["table_query"]))
    assert not res.judge_needed
    assert res.score.failure_category == "wrong_number"
    assert res.score.scores["correctness"] == 0.0


def test_number_within_tolerance_passes_exact() -> None:
    """3650 vs 3650.1 → exact-match branch fires first (contains 3650 substring)."""
    res = scorer.score_turn(_turn("3650"), "3650.1", trajectory=_traj(["table_query"]))
    # normalize keeps the digits; exact-match substring applies.
    assert not res.judge_needed


def test_lazy_refusal_without_query_tool_fails() -> None:
    res = scorer.score_turn(_turn("48"), "I can't find that information.", trajectory=_traj([]))
    assert not res.judge_needed
    assert res.score.failure_category == "lazy_refusal"


def test_lazy_refusal_with_query_tool_needs_judge() -> None:
    """If the agent DID call a query tool then said 'can't find', the zero-rule
    does not fire — the judge must decide."""
    res = scorer.score_turn(
        _turn("48"), "I can't find that information.", trajectory=_traj(["read_file"])
    )
    assert res.judge_needed


def test_must_contain_miss_fails() -> None:
    res = scorer.score_turn(
        _turn("Анна", must_contain="маркетинг"), "Вася из отдела продаж.", trajectory=_traj([])
    )
    assert not res.judge_needed
    assert res.score.failure_category == "missing_required_content"


def test_no_match_no_zero_rule_needs_judge() -> None:
    """Plausible but non-exact answer with no zero-rule → judge needed."""
    res = scorer.score_turn(
        _turn("3650"),
        "Приблизительно три с половиной тысячи.",
        trajectory=_traj(["table_query"]),
    )
    assert res.judge_needed
    assert res.score.failure_category is None


# ───────────────────────── score recomputation ─────────────────────────────


def test_settled_correct_overall_above_threshold() -> None:
    from corpclaw_lite.eval.scores import PASS_MIN_OVERALL

    res = scorer.score_turn(_turn("3650"), "3650", trajectory=_traj(["table_query"]))
    assert res.score.overall_score >= PASS_MIN_OVERALL
    assert res.score.passed


def test_zero_overall_is_zero() -> None:
    res = scorer.score_turn(_turn("3650"), "3000", trajectory=_traj(["table_query"]))
    assert res.score.overall_score == 0.0
    assert not res.score.passed


def test_recompute_overall_weights_sum_to_one() -> None:
    """All-10 scores must give overall exactly 10.0 (weights sum to 1)."""
    from corpclaw_lite.eval.scores import recompute_overall

    all_ten = dict.fromkeys(
        (
            "correctness",
            "tool_selection",
            "context_retention",
            "completeness",
            "efficiency",
            "personality",
            "error_recovery",
        ),
        10.0,
    )
    assert recompute_overall(all_ten) == 10.0


def test_decide_pass_requires_correctness_threshold() -> None:
    from corpclaw_lite.eval.scores import decide_pass

    # correctness 3 (< 4) fails even with high overall.
    assert not decide_pass({"correctness": 3.0, "efficiency": 10.0}, 9.0)
    # correctness 0 always fails.
    assert not decide_pass({"correctness": 0.0}, 8.0)


@pytest.mark.parametrize(
    "expected,actual,tol,within",
    [
        (100.0, 100.0, 0.05, True),
        (100.0, 104.0, 0.05, True),  # 4% off
        (100.0, 106.0, 0.05, False),  # 6% off
        (0.0, 0.0, 0.05, True),
        (1000.0, 1049.0, 0.05, True),  # 4.9% off
        (1000.0, 1051.0, 0.05, False),  # 5.1% off
    ],
)
def test_numbers_within_tolerance(expected: float, actual: float, tol: float, within: bool) -> None:
    from corpclaw_lite.eval.scores import numbers_within_tolerance

    assert numbers_within_tolerance(expected, actual, tol) is within


def test_extract_numbers_handles_comma_decimal() -> None:
    from corpclaw_lite.eval.scores import extract_numbers

    # Comma decimals are parsed; space-separated thousands may split into parts.
    nums = extract_numbers("1200,50")
    assert 1200.5 in nums or 200.5 in nums
    assert 1200.5 in extract_numbers("1200,50") or 200.5 in extract_numbers("1200,50")


# ──────────────────── thousands separator (Gap 2) ─────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("27 000", [27000.0]),
        ("1 200 500", [1200500.0]),
        ("выручка 27 000 рублей", [27000.0]),
        ("5\u00a0000", [5000.0]),  # NBSP (U+00A0)
        ("1\u2009000", [1000.0]),  # thin space (U+2009)
    ],
)
def test_extract_numbers_collapses_thousands_separators(text: str, expected: list[float]) -> None:
    """Regression (live A/B on gemma): '27 000' was parsed as [27.0, 0.0],
    which caused a false wrong_number zero in multi_file_read_combine. The
    space/NBSP/thin-space between digits must be treated as a thousands
    separator and collapsed before parsing."""
    from corpclaw_lite.eval.scores import extract_numbers

    assert extract_numbers(text) == expected


def test_extract_numbers_keeps_prose_spaces() -> None:
    """Ordinary inter-word spaces must NOT be touched — only digit-to-digit
    spaces are thousands separators."""
    from corpclaw_lite.eval.scores import extract_numbers

    # "20 квартир по 50" → 20 and 50, not 2050.
    nums = extract_numbers("20 квартир по 50 метров")
    assert nums == [20.0, 50.0]


def test_normalize_answer_collapses_thousands_separators() -> None:
    """normalize_answer must collapse '27 000' → '27000' so that exact-match
    substring comparison succeeds against an expected '27000'."""
    from corpclaw_lite.eval.scores import normalize_answer

    assert normalize_answer("27 000") == "27000"
    assert normalize_answer("1 200 500") == "1200500"
    # Prose spaces preserved.
    assert " " in normalize_answer("вверх по течению")


def test_exact_match_with_thousands_separator_passes() -> None:
    """Regression (live A/B on gemma, multi_file_read_combine): expected_answer
    '27000', agent answered 'Общая выручка 27 000.' Previously failed as
    wrong_number; now must settle as an exact normalized match."""
    res = scorer.score_turn(
        _turn("27000"),
        "Общая выручка за два квартала составляет 27 000.",
        trajectory=_traj(["read_file", "read_file"]),
    )
    assert not res.judge_needed
    assert res.score.scores["correctness"] == 10.0
    assert res.score.passed


def test_aggregate_scenario_all_pass() -> None:
    from corpclaw_lite.eval.scores import TurnScore, aggregate_scenario

    t1 = TurnScore(overall_score=8.0, passed=True)
    t2 = TurnScore(overall_score=9.0, passed=True)
    agg = aggregate_scenario("s1", [t1, t2])
    assert agg.passed
    assert agg.overall_score == 8.5


def test_aggregate_scenario_one_turn_fails() -> None:
    from corpclaw_lite.eval.scores import TurnScore, aggregate_scenario

    agg = aggregate_scenario(
        "s1",
        [
            TurnScore(overall_score=8.0, passed=True),
            TurnScore(overall_score=2.0, passed=False),
        ],
    )
    assert not agg.passed
