"""Tests for the LLM judge (B-060, step 4).

The provider is mocked with canned responses; no real cloud call is made. The
tests pin prompt assembly, JSON verdict parsing (including tolerating markdown
fences and extracting {...} blocks), score recomputation (LLM's overall_score
is ignored — the harness recomputes from dimensions), and error handling.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from corpclaw_lite.calibration.trajectory import Trajectory, TrajectoryRecorder, TrajectoryStep
from corpclaw_lite.eval.judge import JudgeError, LLMJudge, _strip_code_fence, render_transcript
from corpclaw_lite.eval.scenarios import ScenarioTurn
from corpclaw_lite.llm.base import LLMResponse, Provider


class _CannedProvider(Provider):
    """Returns a single canned response, capturing the last prompt."""

    def __init__(self, response_content: str) -> None:
        self._response = response_content
        self.last_messages: list[dict[str, Any]] | None = None
        self.last_system: str | None = None
        self.call_count = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        self.last_messages = messages
        self.last_system = system
        self.call_count += 1
        return LLMResponse(content=self._response)


def _passing_verdict() -> str:
    return json.dumps(
        {
            "scores": {
                "correctness": 9,
                "tool_selection": 9,
                "context_retention": 9,
                "completeness": 9,
                "efficiency": 9,
                "personality": 9,
                "error_recovery": 9,
            },
            "overall_score": 9.0,
            "pass": True,
            "failure_category": None,
            "reasoning": "Good answer.",
        }
    )


def _trajectory_with_tools() -> Trajectory:
    rec = TrajectoryRecorder("s1")
    rec.record_tool_call("table_query", {"query": "SELECT SUM(revenue) FROM t"})
    rec.record_tool_result("table_query", "3650")
    return rec.finalize("3650", iterations=2, tools_used=["table_query"])


def _empty_trajectory() -> Trajectory:
    return Trajectory(scenario_id="s1")


@pytest.mark.asyncio
async def test_judge_parses_clean_json() -> None:
    provider = _CannedProvider(_passing_verdict())
    judge = LLMJudge(provider)
    score = await judge.judge_turn(
        ScenarioTurn(user_message="q", expected_answer="3650"),
        final_answer="3650",
        trajectory=_trajectory_with_tools(),
    )
    assert score.judge_used is True
    assert score.scores["correctness"] == 9.0
    assert score.reasoning == "Good answer."
    assert score.failure_category is None
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_judge_recomputes_overall_ignoring_llm_value() -> None:
    """The harness recomputes overall from dimensions — the LLM's reported
    overall_score must NOT be trusted."""
    # LLM claims overall 9.9 but the dimensions average to 9.0.
    verdict = json.loads(_passing_verdict())
    verdict["overall_score"] = 9.9
    provider = _CannedProvider(json.dumps(verdict))
    judge = LLMJudge(provider)
    score = await judge.judge_turn(
        ScenarioTurn(user_message="q", expected_answer="3650"),
        final_answer="3650",
        trajectory=_trajectory_with_tools(),
    )
    # 9.0 = 9 * sum(weights) = 9.0, NOT 9.9.
    assert score.overall_score == 9.0


@pytest.mark.asyncio
async def test_judge_tolerates_markdown_fence() -> None:
    fenced = "```json\n" + _passing_verdict() + "\n```"
    provider = _CannedProvider(fenced)
    judge = LLMJudge(provider)
    score = await judge.judge_turn(
        ScenarioTurn(user_message="q", expected_answer="3650"),
        final_answer="3650",
        trajectory=_trajectory_with_tools(),
    )
    assert score.scores["correctness"] == 9.0


@pytest.mark.asyncio
async def test_judge_extracts_json_from_prose() -> None:
    """Judge wraps JSON in prose — the {...} fallback must extract it."""
    verdict = (
        "Here is my verdict.\n\n" + _passing_verdict() + "\n\nLet me know if you need more detail."
    )
    provider = _CannedProvider(verdict)
    judge = LLMJudge(provider)
    score = await judge.judge_turn(
        ScenarioTurn(user_message="q", expected_answer="3650"),
        final_answer="3650",
        trajectory=_trajectory_with_tools(),
    )
    assert score.scores["correctness"] == 9.0


@pytest.mark.asyncio
async def test_judge_pass_fail_decision_recomputed() -> None:
    """Even if the LLM says pass=True, a low correctness must yield pass=False."""
    verdict = json.loads(_passing_verdict())
    verdict["scores"]["correctness"] = 2  # below threshold
    verdict["pass"] = True  # LLM wrongly says pass
    provider = _CannedProvider(json.dumps(verdict))
    judge = LLMJudge(provider)
    score = await judge.judge_turn(
        ScenarioTurn(user_message="q", expected_answer="3650"),
        final_answer="maybe",
        trajectory=_trajectory_with_tools(),
    )
    assert score.passed is False  # recomputed, not trusted


@pytest.mark.asyncio
async def test_judge_missing_dimension_raises() -> None:
    verdict = json.loads(_passing_verdict())
    del verdict["scores"]["correctness"]
    provider = _CannedProvider(json.dumps(verdict))
    judge = LLMJudge(provider)
    with pytest.raises(JudgeError, match="missing dimension"):
        await judge.judge_turn(
            ScenarioTurn(user_message="q", expected_answer="3650"),
            final_answer="x",
            trajectory=_trajectory_with_tools(),
        )


@pytest.mark.asyncio
async def test_judge_non_numeric_score_raises() -> None:
    verdict = json.loads(_passing_verdict())
    verdict["scores"]["correctness"] = "excellent"
    provider = _CannedProvider(json.dumps(verdict))
    judge = LLMJudge(provider)
    with pytest.raises(JudgeError, match="not numeric"):
        await judge.judge_turn(
            ScenarioTurn(user_message="q", expected_answer="3650"),
            final_answer="x",
            trajectory=_trajectory_with_tools(),
        )


@pytest.mark.asyncio
async def test_judge_unparseable_content_raises() -> None:
    provider = _CannedProvider("this is not json at all")
    judge = LLMJudge(provider)
    with pytest.raises(JudgeError, match="unparseable"):
        await judge.judge_turn(
            ScenarioTurn(user_message="q", expected_answer="3650"),
            final_answer="x",
            trajectory=_trajectory_with_tools(),
        )


@pytest.mark.asyncio
async def test_prompt_contains_rubric_scenario_and_transcript() -> None:
    """The assembled prompt must include the rubric text, the scenario
    expected_answer, and the rendered tool-call transcript."""
    provider = _CannedProvider(_passing_verdict())
    judge = LLMJudge(provider)
    await judge.judge_turn(
        ScenarioTurn(
            user_message="What is the total?",
            expected_answer="3650",
            expected_tools=["table_query"],
            success_criteria="Use SQL aggregation.",
        ),
        final_answer="3650",
        trajectory=_trajectory_with_tools(),
    )
    prompt = provider.last_messages[0]["content"]  # type: ignore[index]
    assert "judge_turn" in prompt.lower() or "STEP 1" in prompt  # rubric present
    assert "3650" in prompt  # expected answer
    assert "table_query" in prompt  # expected tool + transcript
    assert "SELECT SUM(revenue)" in prompt  # transcript tool args
    assert "Use SQL aggregation" in prompt  # success criteria


@pytest.mark.asyncio
async def test_judge_prompt_contains_tool_surface_when_provided() -> None:
    """When agent_tools is set, the prompt must warn the judge that execution
    tools are subagent-only, so it does not penalise delegation."""
    provider = _CannedProvider(_passing_verdict())
    judge = LLMJudge(
        provider,
        agent_tools=[
            "read_file",
            "list_files",
            "search_files",
            "excel_inspect",
            "dispatch_subagent",
            "web_fetch",
            "read_image",
            "memory_store",
            "memory_recall",
        ],
    )
    await judge.judge_turn(
        ScenarioTurn(user_message="x", expected_answer="3650"),
        final_answer="3650",
        trajectory=_trajectory_with_tools(),
    )
    prompt = provider.last_messages[0]["content"]  # type: ignore[index]
    assert "Tools Available to the Agent" in prompt
    assert "dispatch_subagent" in prompt
    assert "NOT directly available" in prompt  # delegation warning


@pytest.mark.asyncio
async def test_judge_prompt_omits_tool_surface_when_not_provided() -> None:
    provider = _CannedProvider(_passing_verdict())
    judge = LLMJudge(provider)
    await judge.judge_turn(
        ScenarioTurn(user_message="x", expected_answer="3650"),
        final_answer="3650",
        trajectory=_empty_trajectory(),
    )
    prompt = provider.last_messages[0]["content"]  # type: ignore[index]
    assert "Tools Available to the Agent" not in prompt


@pytest.mark.asyncio
async def test_transcript_renders_nested_subagent_calls_with_prefix() -> None:
    """Nested subagent tool calls must be prefixed with the subagent id."""
    from corpclaw_lite.calibration.trajectory import Trajectory, TrajectoryStep

    provider = _CannedProvider(_passing_verdict())
    judge = LLMJudge(provider)
    traj = Trajectory(
        scenario_id="s1",
        steps=[
            TrajectoryStep(
                step_type="tool_call",
                tool_name="dispatch_subagent",
                tool_args={"subagent_id": "data-agent"},
            ),
            TrajectoryStep(
                step_type="tool_call",
                tool_name="table_query",
                tool_args={"query": "SELECT 1"},
                subagent_id="data-agent",
            ),
        ],
    )
    await judge.judge_turn(
        ScenarioTurn(user_message="x", expected_answer="3650"),
        final_answer="3650",
        trajectory=traj,
    )
    prompt = provider.last_messages[0]["content"]  # type: ignore[index]
    assert "[data-agent]" in prompt  # nested prefix present
    assert "table_query" in prompt


@pytest.mark.asyncio
async def test_prompt_null_expected_answer_rendered() -> None:
    provider = _CannedProvider(_passing_verdict())
    judge = LLMJudge(provider)
    await judge.judge_turn(
        ScenarioTurn(user_message="q", expected_answer=None),
        final_answer="I don't know.",
        trajectory=_empty_trajectory(),
    )
    prompt = provider.last_messages[0]["content"]  # type: ignore[index]
    assert "null" in prompt.lower() or "does NOT exist" in prompt


@pytest.mark.asyncio
async def test_transcript_empty_for_direct_answer() -> None:
    """A trajectory with no tool calls renders as 'answered directly'."""
    provider = _CannedProvider(_passing_verdict())
    judge = LLMJudge(provider)
    await judge.judge_turn(
        ScenarioTurn(user_message="q", expected_answer="3650"),
        final_answer="3650",
        trajectory=_empty_trajectory(),
    )
    prompt = provider.last_messages[0]["content"]  # type: ignore[index]
    assert "no tool calls" in prompt.lower()


@pytest.mark.asyncio
async def test_transcript_truncates_long_tool_results() -> None:
    rec = TrajectoryRecorder("s1")
    rec.record_tool_call("table_query", {"query": "SELECT * FROM big"})
    rec.record_tool_result("table_query", "x" * 1000)
    traj = rec.finalize("done")
    provider = _CannedProvider(_passing_verdict())
    judge = LLMJudge(provider)
    await judge.judge_turn(
        ScenarioTurn(user_message="q", expected_answer="done"),
        final_answer="done",
        trajectory=traj,
    )
    prompt = provider.last_messages[0]["content"]  # type: ignore[index]
    # Result truncated to ~300 chars + ellipsis.
    assert "…" in prompt
    assert "x" * 400 not in prompt


def test_strip_code_fence_removes_json_fence() -> None:
    assert _strip_code_fence('```json\n{"a":1}\n```') == '{"a":1}'


def test_strip_code_fence_passthrough_without_fence() -> None:
    assert _strip_code_fence('{"a":1}') == '{"a":1}'


def test_judge_missing_rubric_file_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(FileNotFoundError):
        LLMJudge(_CannedProvider(""), rubric_path=tmp_path / "nope.md")


def test_trajectory_step_truncation_independent_of_judge() -> None:
    """Trajectory.to_dict truncates tool_result to 500 chars; the judge renders
    its own shorter view. The two are independent."""
    step = TrajectoryStep(step_type="tool_result", tool_name="t", tool_result="y" * 800)
    assert len(step.tool_result) == 800  # raw field untruncated


def test_render_transcript_empty_trajectory() -> None:
    traj = Trajectory(scenario_id="s1")
    assert "no tool calls" in render_transcript(traj)


def test_render_transcript_with_tool_calls() -> None:
    rec = TrajectoryRecorder("s1")
    rec.record_tool_call("read_file", {"path": "data.txt"})
    rec.record_tool_result("read_file", "hello world")
    traj = rec.finalize("done")
    rendered = render_transcript(traj)
    assert "read_file" in rendered
    assert "hello world" in rendered


def test_render_transcript_with_nested_subagent() -> None:
    rec = TrajectoryRecorder("s1")
    rec.record_tool_call("dispatch_subagent", {"subagent_id": "data-agent"})
    rec.record_nested(
        "data-agent",
        [
            TrajectoryStep(step_type="tool_call", tool_name="table_query"),
        ],
    )
    traj = rec.finalize("done")
    rendered = render_transcript(traj)
    assert "[data-agent]" in rendered
    assert "table_query" in rendered
