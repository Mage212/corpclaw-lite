"""Tests for nested (subagent) trajectory capture (B-060 router+executor fix).

The router+executor architecture (D-028) delegates execution to subagents. The
main agent's trajectory must therefore show the subagent's inner tool calls
(nested steps), so the eval harness can score the real execution path rather
than only the ``dispatch_subagent`` call.
"""

from __future__ import annotations

from corpclaw_lite.calibration.trajectory import (
    Trajectory,
    TrajectoryRecorder,
    TrajectoryStep,
)
from corpclaw_lite.eval.scenarios import ScenarioTurn
from corpclaw_lite.eval.scorer import DeterministicScorer

scorer = DeterministicScorer()


def _traj(steps: list[TrajectoryStep]) -> Trajectory:
    return Trajectory(scenario_id="t", steps=steps)


# ─────────────────────────── record_nested ──────────────────────────────────


def test_record_nested_merges_inner_steps() -> None:
    parent = TrajectoryRecorder("parent")
    parent.record_tool_call("dispatch_subagent", {"subagent_id": "data-agent", "task": "sum"})
    inner = [
        TrajectoryStep(step_type="tool_call", tool_name="table_query"),
        TrajectoryStep(step_type="tool_result", tool_name="table_query", tool_result="3650"),
    ]
    parent.record_nested("data-agent", inner)
    traj = parent.finalize("3650")
    # Parent sees: dispatch_subagent, [nested] table_query, [nested] result, final.
    assert len(traj.steps) == 4
    assert traj.steps[0].tool_name == "dispatch_subagent"
    assert traj.steps[1].tool_name == "table_query"
    assert traj.steps[1].subagent_id == "data-agent"
    assert traj.steps[2].subagent_id == "data-agent"


def test_tool_calls_sequence_excludes_subagent_by_default() -> None:
    """Backward-compat: calibration scorer sees only main-agent calls."""
    traj = _traj(
        [
            TrajectoryStep(step_type="tool_call", tool_name="dispatch_subagent"),
            TrajectoryStep(
                step_type="tool_call", tool_name="table_query", subagent_id="data-agent"
            ),
            TrajectoryStep(step_type="tool_call", tool_name="read_file", subagent_id="data-agent"),
        ]
    )
    assert traj.tool_calls_sequence() == ["dispatch_subagent"]


def test_tool_calls_sequence_includes_subagent_when_requested() -> None:
    traj = _traj(
        [
            TrajectoryStep(step_type="tool_call", tool_name="dispatch_subagent"),
            TrajectoryStep(
                step_type="tool_call", tool_name="table_query", subagent_id="data-agent"
            ),
        ]
    )
    assert traj.tool_calls_sequence(include_subagent=True) == ["dispatch_subagent", "table_query"]


def test_dispatch_subagent_ids_extracts_args() -> None:
    traj = _traj(
        [
            TrajectoryStep(
                step_type="tool_call",
                tool_name="dispatch_subagent",
                tool_args={"subagent_id": "data-agent", "task": "x"},
            ),
            TrajectoryStep(
                step_type="tool_call",
                tool_name="dispatch_subagent",
                tool_args={"subagent_id": "document-agent", "task": "y"},
            ),
        ]
    )
    assert traj.dispatch_subagent_ids() == ["data-agent", "document-agent"]


def test_dispatch_subagent_ids_ignores_non_dispatch_calls() -> None:
    traj = _traj(
        [
            TrajectoryStep(step_type="tool_call", tool_name="read_file"),
            TrajectoryStep(
                step_type="tool_call",
                tool_name="dispatch_subagent",
                tool_args={"subagent_id": "data-agent"},
            ),
        ]
    )
    assert traj.dispatch_subagent_ids() == ["data-agent"]


def test_to_dict_includes_subagent_id_field() -> None:
    traj = _traj(
        [
            TrajectoryStep(
                step_type="tool_call", tool_name="table_query", subagent_id="data-agent"
            ),
        ]
    )
    d = traj.to_dict()
    assert d["steps"][0]["subagent_id"] == "data-agent"


# ──────────────────── expected_subagent scoring ─────────────────────────────


def test_expected_subagent_correct_delegation_with_answer_passes() -> None:
    """expected_answer + correct delegation → exact match settles correct."""
    turn = ScenarioTurn(
        user_message="sum the revenue",
        expected_answer="3650",
        expected_subagent="data-agent",
    )
    traj = _traj(
        [
            TrajectoryStep(
                step_type="tool_call",
                tool_name="dispatch_subagent",
                tool_args={"subagent_id": "data-agent", "task": "sum revenue"},
            ),
        ]
    )
    res = scorer.score_turn(turn, "3650", traj)
    assert not res.judge_needed
    assert res.score.scores["correctness"] == 10.0


def test_expected_answer_correct_without_delegation_still_passes() -> None:
    """A correct answer solved via inspection tools alone is NOT failed just
    because the agent didn't delegate. Delegation is a tool_selection/efficiency
    concern, scored by the judge — not a correctness gate."""
    turn = ScenarioTurn(
        user_message="sum the revenue",
        expected_answer="3650",
        expected_subagent="data-agent",
    )
    traj = _traj([TrajectoryStep(step_type="tool_call", tool_name="read_file")])
    res = scorer.score_turn(turn, "3650", traj)
    # Exact match → correctness 10 regardless of delegation.
    assert not res.judge_needed
    assert res.score.scores["correctness"] == 10.0


def test_behavioural_expected_subagent_correct_delegation_settles() -> None:
    """No expected_answer + expected_subagent + correct delegation → settled
    correct (the delegation path is the signal being graded)."""
    turn = ScenarioTurn(
        user_message="create a file",
        expected_subagent="filesystem-agent",
    )
    traj = _traj(
        [
            TrajectoryStep(
                step_type="tool_call",
                tool_name="dispatch_subagent",
                tool_args={"subagent_id": "filesystem-agent", "task": "create file"},
            ),
        ]
    )
    res = scorer.score_turn(turn, "Created todo.txt.", traj)
    assert not res.judge_needed
    assert res.score.scores["correctness"] == 10.0


def test_behavioural_expected_subagent_wrong_delegation_defers_to_judge() -> None:
    """No expected_answer + wrong delegation → judge decides. Not a zero-rule."""
    turn = ScenarioTurn(
        user_message="create a file",
        expected_subagent="filesystem-agent",
    )
    traj = _traj(
        [
            TrajectoryStep(
                step_type="tool_call",
                tool_name="dispatch_subagent",
                tool_args={"subagent_id": "research-agent", "task": "..."},
            ),
        ]
    )
    res = scorer.score_turn(turn, "Created todo.txt.", traj)
    assert res.judge_needed
    assert res.score.failure_category is None


def test_no_expected_subagent_no_delegation_check() -> None:
    """When expected_subagent is None, delegation is not enforced."""
    turn = ScenarioTurn(user_message="x", expected_answer="42")
    traj = _traj([TrajectoryStep(step_type="tool_call", tool_name="read_file")])
    res = scorer.score_turn(turn, "42", traj)
    assert res.score.scores["correctness"] == 10.0
