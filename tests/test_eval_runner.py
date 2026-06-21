"""Tests for the eval runner (B-060, step 5).

The AgentLoop is faked (no real LLM call); the runner's orchestration —
multi-turn execution, workspace materialise/cleanup, memory isolation between
scenarios, and the deterministic→judge scoring pipeline — is what's under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from corpclaw_lite.eval.runner import EvalRunner
from corpclaw_lite.eval.scenarios import EvalScenario, ScenarioSetup, ScenarioTurn
from corpclaw_lite.llm.base import LLMResponse, Provider
from corpclaw_lite.users.models import User


class _FakeMemory:
    def __init__(self) -> None:
        self.cleared: list[str] = []
        self.facts_cleared: list[str] = []

    async def clear(self, user_id: str) -> None:
        self.cleared.append(user_id)

    async def clear_facts(self, user_id: str) -> None:
        self.facts_cleared.append(user_id)


class _FakeRunStats:
    def __init__(
        self, iterations: int = 1, tools: list[str] | None = None, status: str = "ok"
    ) -> None:
        self.iterations = iterations
        self.tools_used = tools or []
        self.duration_ms = 100.0
        self.status = status


class _FakeAgentLoop:
    """Fakes AgentLoop.run() with a scripted sequence of answers per call."""

    def __init__(self, answers: list[str], tools_per_call: list[list[str]] | None = None) -> None:
        self._answers = list(answers)
        self._tools = tools_per_call or []
        self.memory = _FakeMemory()
        self.calls: list[str] = []

    async def run(self, **kwargs: Any) -> tuple[str, _FakeRunStats]:
        message = kwargs["message"]
        self.calls.append(message)
        answer = self._answers.pop(0) if self._answers else "(no more scripted answers)"
        tools = self._tools.pop(0) if self._tools else []
        return answer, _FakeRunStats(tools=tools)


class _CannedProvider(Provider):
    def __init__(self, content: str) -> None:
        self._content = content

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(content=self._content)


def _user() -> User:
    return User(id=99, name="Eval", department="engineering")


def _passing_judge_verdict() -> str:
    dims = (
        "correctness",
        "tool_selection",
        "context_retention",
        "completeness",
        "efficiency",
        "personality",
        "error_recovery",
    )
    return json.dumps(
        {
            "scores": dict.fromkeys(dims, 9),
            "overall_score": 9.0,
            "pass": True,
            "failure_category": None,
            "reasoning": "ok",
        }
    )


# ──────────────────────────── single-turn flow ─────────────────────────────


@pytest.mark.asyncio
async def test_single_turn_exact_match_settles_without_judge(tmp_path: Path) -> None:
    """Deterministic exact-match settles correctness=10; judge not consulted."""
    loop = _FakeAgentLoop(answers=["3650"], tools_per_call=[["table_query"]])
    runner = EvalRunner(loop, _user(), system_prompt="sys", workspace_dir=tmp_path)
    scenario = EvalScenario(
        id="s1",
        category="office",
        turns=[ScenarioTurn(user_message="total?", expected_answer="3650")],
    )
    scores = await runner.run_all([scenario])
    assert len(scores) == 1
    assert scores[0].passed
    assert scores[0].overall_score >= 6.0
    assert scores[0].turns[0].scores["correctness"] == 10.0


@pytest.mark.asyncio
async def test_single_turn_zero_rule_settles_without_judge(tmp_path: Path) -> None:
    """Wrong-number zero-rule fires; judge not consulted."""
    loop = _FakeAgentLoop(answers=["9999"], tools_per_call=[["table_query"]])
    runner = EvalRunner(loop, _user(), system_prompt="sys", workspace_dir=tmp_path)
    scenario = EvalScenario(
        id="s1",
        category="office",
        turns=[ScenarioTurn(user_message="total?", expected_answer="3650")],
    )
    scores = await runner.run_all([scenario])
    assert not scores[0].passed
    assert scores[0].turns[0].failure_category == "wrong_number"


@pytest.mark.asyncio
async def test_judge_invoked_when_deterministic_cannot_settle(tmp_path: Path) -> None:
    """A plausible but non-exact answer with no zero-rule → judge is called."""
    from corpclaw_lite.eval.judge import LLMJudge

    loop = _FakeAgentLoop(answers=["приблизительно три тысячи"], tools_per_call=[["table_query"]])
    judge = LLMJudge(_CannedProvider(_passing_judge_verdict()))
    runner = EvalRunner(loop, _user(), "sys", tmp_path, judge=judge)
    scenario = EvalScenario(
        id="s1",
        category="office",
        turns=[ScenarioTurn(user_message="total?", expected_answer="3650")],
    )
    scores = await runner.run_all([scenario])
    assert scores[0].turns[0].judge_used is True
    assert scores[0].turns[0].scores["correctness"] == 9.0


@pytest.mark.asyncio
async def test_no_judge_fallback_when_judge_missing(tmp_path: Path) -> None:
    """Without a judge, a non-settling turn gets conservative fallback scores."""
    loop = _FakeAgentLoop(answers=["приблизительно три тысячи"], tools_per_call=[["table_query"]])
    runner = EvalRunner(loop, _user(), "sys", tmp_path)
    scenario = EvalScenario(
        id="s1",
        category="office",
        turns=[ScenarioTurn(user_message="total?", expected_answer="3650")],
    )
    scores = await runner.run_all([scenario])
    ts = scores[0].turns[0]
    assert ts.scores["correctness"] == 5.0
    assert "fallback" in ts.reasoning.lower()


# ──────────────────────────── multi-turn flow ──────────────────────────────


@pytest.mark.asyncio
async def test_multi_turn_runs_each_turn_and_aggregates(tmp_path: Path) -> None:
    loop = _FakeAgentLoop(
        answers=["20", "35000"],
        tools_per_call=[["read_file"], []],
    )
    runner = EvalRunner(loop, _user(), "sys", tmp_path)
    scenario = EvalScenario(
        id="multi",
        category="retention",
        turns=[
            ScenarioTurn(user_message="YoY growth?", expected_answer="20"),
            ScenarioTurn(user_message="Q4 revenue?", expected_answer="35000"),
        ],
    )
    scores = await runner.run_all([scenario])
    assert len(scores[0].turns) == 2
    # Each turn settled by exact match.
    assert all(t.scores["correctness"] == 10.0 for t in scores[0].turns)
    assert scores[0].passed
    # Both turn messages were sent to the loop.
    assert loop.calls == ["YoY growth?", "Q4 revenue?"]


@pytest.mark.asyncio
async def test_multi_turn_one_fails_scenario_fails(tmp_path: Path) -> None:
    loop = _FakeAgentLoop(answers=["20", "999"], tools_per_call=[["read_file"], []])
    runner = EvalRunner(loop, _user(), "sys", tmp_path)
    scenario = EvalScenario(
        id="multi",
        category="retention",
        turns=[
            ScenarioTurn(user_message="q1", expected_answer="20"),
            ScenarioTurn(user_message="q2", expected_answer="35000"),
        ],
    )
    scores = await runner.run_all([scenario])
    assert not scores[0].passed  # turn 2 wrong-number fails


@pytest.mark.asyncio
async def test_crashed_turn_zeros_and_stops_scenario(tmp_path: Path) -> None:
    loop = _FakeAgentLoop(answers=["20", "35000"])
    # Force the second run() call to raise.
    original_run = loop.run
    call_count = {"n": 0}

    async def crashing_run(**kwargs: Any) -> tuple[str, _FakeRunStats]:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("boom")
        return await original_run(**kwargs)

    loop.run = crashing_run  # type: ignore[method-assign]
    runner = EvalRunner(loop, _user(), "sys", tmp_path)
    scenario = EvalScenario(
        id="multi",
        category="retention",
        turns=[
            ScenarioTurn(user_message="q1", expected_answer="20"),
            ScenarioTurn(user_message="q2", expected_answer="35000"),
        ],
    )
    scores = await runner.run_all([scenario])
    # The first turn ran and passed; the second crashed and is recorded as a
    # zero-scored run_error turn, after which the scenario stops (no turn 3).
    assert len(scores[0].turns) == 2
    assert scores[0].turns[0].passed
    crashed = scores[0].turns[1]
    assert not crashed.passed
    assert crashed.failure_category == "run_error"
    assert not scores[0].passed


# ──────────────────────────── workspace mgmt ───────────────────────────────


@pytest.mark.asyncio
async def test_workspace_files_materialised_and_cleaned(tmp_path: Path) -> None:
    loop = _FakeAgentLoop(answers=["done"], tools_per_call=[["read_file"]])
    runner = EvalRunner(loop, _user(), "sys", tmp_path)
    scenario = EvalScenario(
        id="s1",
        category="office",
        setup=ScenarioSetup(files=[("data.txt", "hello world")]),
        turns=[ScenarioTurn(user_message="read data.txt")],
    )
    # Before run, file does not exist.
    assert not (tmp_path / "data.txt").exists()
    await runner.run_all([scenario])
    # After run, file cleaned up.
    assert not (tmp_path / "data.txt").exists()


@pytest.mark.asyncio
async def test_corpus_fixture_copied(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    fixture = corpus / "blob.xlsx"
    fixture.write_bytes(b"fake xlsx")
    ws = tmp_path / "ws"
    ws.mkdir()
    loop = _FakeAgentLoop(answers=["done"], tools_per_call=[["excel_workbook"]])
    runner = EvalRunner(loop, _user(), "sys", ws, corpus_dir=corpus)
    scenario = EvalScenario(
        id="s1",
        category="office",
        setup=ScenarioSetup(copy_from_corpus=[("blob.xlsx", "blob.xlsx")]),
        turns=[ScenarioTurn(user_message="inspect")],
    )
    await runner.run_all([scenario])
    assert not (ws / "blob.xlsx").exists()  # cleaned up


@pytest.mark.asyncio
async def test_missing_corpus_fixture_warned_not_fatal(tmp_path: Path) -> None:
    loop = _FakeAgentLoop(answers=["done"], tools_per_call=[])
    runner = EvalRunner(loop, _user(), "sys", tmp_path, corpus_dir=tmp_path / "corpus")
    scenario = EvalScenario(
        id="s1",
        category="office",
        setup=ScenarioSetup(copy_from_corpus=[("blob.xlsx", "missing.xlsx")]),
        turns=[ScenarioTurn(user_message="x")],
    )
    # Should not raise even though the fixture is absent.
    scores = await runner.run_all([scenario])
    assert len(scores) == 1


# ──────────────────────────── isolation ────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_cleared_between_scenarios(tmp_path: Path) -> None:
    loop = _FakeAgentLoop(answers=["a", "b"], tools_per_call=[[], []])
    runner = EvalRunner(loop, _user(), "sys", tmp_path)
    scenarios = [
        EvalScenario(id="s1", category="c", turns=[ScenarioTurn(user_message="m1")]),
        EvalScenario(id="s2", category="c", turns=[ScenarioTurn(user_message="m2")]),
    ]
    await runner.run_all(scenarios)
    # Both conversation (clear) and stored facts (clear_facts) are wiped between
    # scenarios so a memory_store in s1 cannot leak into s2's recall.
    assert loop.memory.cleared == ["99", "99"]
    assert loop.memory.facts_cleared == ["99", "99"]


@pytest.mark.asyncio
async def test_clear_facts_skipped_when_memory_lacks_it(tmp_path: Path) -> None:
    """A memory backend without clear_facts (e.g. a minimal mock) must not crash
    the runner — the hasattr guard skips the call gracefully."""
    loop = _FakeAgentLoop(answers=["a"], tools_per_call=[[]])
    # Remove clear_facts to simulate a backend that doesn't implement it.
    delattr(loop.memory, "facts_cleared")
    del type(loop.memory).clear_facts  # type: ignore[attr-defined]
    runner = EvalRunner(loop, _user(), "sys", tmp_path)
    scenarios = [EvalScenario(id="s1", category="c", turns=[ScenarioTurn(user_message="m")])]
    # Should not raise.
    await runner.run_all(scenarios)
    assert loop.memory.cleared == ["99"]


@pytest.mark.asyncio
async def test_chdir_into_workspace_during_run(tmp_path: Path) -> None:
    """Regression: the agent's file tools resolve against os.getcwd(), so the
    runner MUST chdir into the workspace for the duration of the run — otherwise
    setup files written to workspace_dir are invisible to the agent."""
    import os

    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen_cwds: list[str] = []

    class _CwdCapturingLoop(_FakeAgentLoop):
        async def run(self, **kwargs: Any) -> tuple[str, _FakeRunStats]:
            seen_cwds.append(os.getcwd())
            return await super().run(**kwargs)

    loop = _CwdCapturingLoop(answers=["done"], tools_per_call=[["table_query"]])
    runner = EvalRunner(loop, _user(), "sys", workspace)
    scenario = EvalScenario(
        id="s1",
        category="c",
        turns=[ScenarioTurn(user_message="m")],
    )
    original_cwd = os.getcwd()
    await runner.run_all([scenario])
    # During the run, cwd was the workspace (resolved, so compare real paths).
    import pathlib

    assert pathlib.Path(seen_cwds[0]).resolve() == workspace.resolve()
    # After the run, the original cwd is restored.
    assert os.getcwd() == original_cwd


@pytest.mark.asyncio
async def test_chdir_restored_even_on_crash(tmp_path: Path) -> None:
    """The original cwd must be restored even if a scenario crashes."""
    import os

    workspace = tmp_path / "ws"
    workspace.mkdir()
    loop = _FakeAgentLoop(answers=[])

    async def crashing_run(**kwargs: Any) -> tuple[str, _FakeRunStats]:
        raise RuntimeError("boom")

    loop.run = crashing_run  # type: ignore[method-assign]
    runner = EvalRunner(loop, _user(), "sys", workspace)
    original_cwd = os.getcwd()
    await runner.run_all(
        [EvalScenario(id="s1", category="c", turns=[ScenarioTurn(user_message="m")])]
    )
    assert os.getcwd() == original_cwd


@pytest.mark.asyncio
async def test_scenario_crash_does_not_abort_run(tmp_path: Path) -> None:
    loop = _FakeAgentLoop(answers=[])
    call_count = {"n": 0}

    async def flaky_run(**kwargs: Any) -> tuple[str, _FakeRunStats]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first scenario explodes")
        return "ok", _FakeRunStats()

    loop.run = flaky_run  # type: ignore[method-assign]
    runner = EvalRunner(loop, _user(), "sys", tmp_path)
    scenarios = [
        EvalScenario(id="s1", category="c", turns=[ScenarioTurn(user_message="m")]),
        EvalScenario(id="s2", category="c", turns=[ScenarioTurn(user_message="m")]),
    ]
    scores = await runner.run_all(scenarios)
    assert len(scores) == 2
    assert not scores[0].passed  # crashed
    assert scores[0].turns[0].failure_category == "run_error"
