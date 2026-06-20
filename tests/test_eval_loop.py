"""Tests for the eval orchestration loop (B-060, step 6).

The AgentStack is faked via monkeypatching ``build_agent_stack`` so no real LLM
provider is needed. The tests pin: single-pass mode writes a PassReport, A/B
mode runs two passes with the guard override applied, and the verdict reflects
the score difference.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from corpclaw_lite.eval import loop as loop_module
from corpclaw_lite.eval.loop import EvalLoop
from corpclaw_lite.eval.scenarios import EvalScenario, ScenarioTurn


class _FakeMemory:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    async def clear(self, user_id: str) -> None:
        self.cleared.append(user_id)


class _FakeStats:
    def __init__(self) -> None:
        self.iterations = 1
        self.tools_used: list[str] = ["table_query"]
        self.duration_ms = 100.0
        self.status = "ok"


class _FakeAgentLoop:
    """Returns a scripted answer; records guard settings seen at construction
    time so the test can assert the A/B override was applied."""

    def __init__(self, answer: str, guard_state_seen: list[str]) -> None:
        self._answer = answer
        self._guard_state_seen = guard_state_seen
        self.memory = _FakeMemory()

    async def run(self, **kwargs: Any) -> tuple[str, _FakeStats]:
        return self._answer, _FakeStats()


class _FakeStack:
    def __init__(self, answer: str, guard_state_seen: list[str]) -> None:
        self.loop = _FakeAgentLoop(answer, guard_state_seen)
        self.few_shots: list[dict[str, Any]] | None = None
        self.tool_registry = None
        self.skill_matcher = None
        self.skill_registry = None


def _patch_orchestration(
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    guard_state_seen: list[str],
    scenarios: list[EvalScenario],
) -> None:
    """Patch build_agent_stack + load_scenarios + bootstrap to avoid real I/O.

    build_agent_stack and BootstrapLoader are imported lazily inside EvalLoop,
    so they must be patched at their source modules. load_scenarios is imported
    at the top of loop.py, so it is patched on the loop module.
    """
    import corpclaw_lite.agent.factory as factory_module
    import corpclaw_lite.config.bootstrap as bootstrap_module

    def fake_build(settings: Any) -> _FakeStack:
        # Record the guard state the loop applied before building the stack.
        guard_state_seen.append(
            f"dedup={settings.agent.result_dedup_guard.enabled},"
            f"planning={settings.agent.planning_text_guard.enabled}"
        )
        return _FakeStack(answer, guard_state_seen)

    monkeypatch.setattr(factory_module, "build_agent_stack", fake_build)
    monkeypatch.setattr(loop_module, "load_scenarios", lambda _path: scenarios)

    class _FakeBootstrap:
        def get_system_prompt(self) -> str:
            return "sys"

    monkeypatch.setattr(bootstrap_module, "BootstrapLoader", lambda *_a, **_kw: _FakeBootstrap())


def _exact_match_scenario(sid: str, expected: str) -> EvalScenario:
    return EvalScenario(
        id=sid,
        category="office",
        turns=[ScenarioTurn(user_message=f"q for {sid}", expected_answer=expected)],
    )


@pytest.mark.asyncio
async def test_single_pass_mode_writes_pass_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    scenarios = [_exact_match_scenario("s1", "3650"), _exact_match_scenario("s2", "100")]
    # Agent answers correctly → exact match → pass.
    _patch_orchestration(monkeypatch, "3650", seen, scenarios)
    # NOTE: the same answer is reused for both scenarios; s2 expects "100" so it
    # will fail exact match but that's fine for the report-shape test.

    ev = EvalLoop(
        ab_guards=False,
        output_dir=tmp_path,
        workspace_base=tmp_path / "ws",
    )
    report = await ev.run()
    assert report is not None
    assert (tmp_path / "eval_report.json").exists()
    assert (tmp_path / "eval_report.md").exists()
    parsed = json.loads((tmp_path / "eval_report.json").read_text(encoding="utf-8"))
    assert "scenarios" in parsed
    # Single pass only built one stack.
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_ab_mode_runs_two_passes_with_guard_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    scenarios = [_exact_match_scenario("s1", "3650")]
    _patch_orchestration(monkeypatch, "3650", seen, scenarios)

    ev = EvalLoop(
        ab_guards=True,
        output_dir=tmp_path,
        workspace_base=tmp_path / "ws",
    )
    ab = await ev.run()
    assert ab is not None
    # Two passes → two stack builds.
    assert len(seen) == 2
    # First pass (guards_on) has both enabled; second (guards_off) disabled.
    assert "dedup=True,planning=True" in seen[0]
    assert "dedup=False,planning=False" in seen[1]
    # Both passes produced a report file.
    assert (tmp_path / "eval_report.json").exists()
    assert (tmp_path / "eval_report.md").exists()


@pytest.mark.asyncio
async def test_ab_verdict_neutral_when_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    scenarios = [_exact_match_scenario("s1", "3650")]
    # Identical correct answers in both passes → neutral.
    _patch_orchestration(monkeypatch, "3650", seen, scenarios)

    ev = EvalLoop(ab_guards=True, output_dir=tmp_path, workspace_base=tmp_path / "ws")
    ab = await ev.run()
    assert ab is not None
    assert ab.verdict == "guards_neutral"
    assert ab.guards_on.passed == ab.guards_off.passed


@pytest.mark.asyncio
async def test_single_pass_with_failing_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    scenarios = [_exact_match_scenario("s1", "3650")]
    # Wrong answer → wrong-number zero-rule → fail.
    _patch_orchestration(monkeypatch, "9999", seen, scenarios)

    ev = EvalLoop(ab_guards=False, output_dir=tmp_path, workspace_base=tmp_path / "ws")
    report = await ev.run()
    assert report is not None
    assert report.total == 1
    assert report.passed == 0
