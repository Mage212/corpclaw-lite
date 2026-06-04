"""Tests for calibration scenarios, scorer, trajectory, and editor."""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from corpclaw_lite.agent.loop import RunStats
from corpclaw_lite.calibration.analyzer import CalibrationAnalyzer
from corpclaw_lite.calibration.editor import ConfigEditor
from corpclaw_lite.calibration.loop import CalibrationLoop
from corpclaw_lite.calibration.runner import CalibrationRunner
from corpclaw_lite.calibration.scenarios import (
    CalibrationScenario,
    ScenarioExpectation,
    ScenarioSetup,
    load_scenarios,
)
from corpclaw_lite.calibration.scorer import CalibrationScorer, ScenarioResult
from corpclaw_lite.calibration.trajectory import Trajectory, TrajectoryRecorder, TrajectoryStep
from corpclaw_lite.config.settings import LLMSettings, RoutingRule, Settings
from corpclaw_lite.llm.base import LLMResponse
from corpclaw_lite.users.models import User

# ═══════════════════════════════════════════════════════════════════════════════
# Scenarios
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadScenarios:
    def test_load_valid_yaml(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            scenarios:
              - id: test1
                user_message: "Read the file"
                category: tool_use
                expected:
                  tool_calls: ["read_file"]
                  has_content: true
              - id: test2
                user_message: "What is 2+2?"
                category: no_tool
                expected:
                  tool_calls: []
                  contains: "4"
        """)
        path = tmp_path / "scenarios.yaml"
        path.write_text(yaml_content)

        scenarios = load_scenarios(path)
        assert len(scenarios) == 2
        assert scenarios[0].id == "test1"
        assert scenarios[0].expected.tool_calls == ["read_file"]
        assert scenarios[1].expected.contains == "4"
        assert scenarios[1].category == "no_tool"

    def test_load_with_setup(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            scenarios:
              - id: with_setup
                user_message: "Read test.txt"
                setup:
                  files:
                    - path: "test.txt"
                      content: "Hello"
                expected:
                  tool_calls: ["read_file"]
        """)
        path = tmp_path / "scenarios.yaml"
        path.write_text(yaml_content)

        scenarios = load_scenarios(path)
        assert scenarios[0].setup is not None
        assert scenarios[0].setup.files == [("test.txt", "Hello")]

    def test_load_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_scenarios("/nonexistent/path.yaml")

    def test_load_empty_scenarios(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_text("scenarios: []")
        with pytest.raises(ValueError, match="No scenarios found"):
            load_scenarios(path)

    def test_load_missing_id(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            scenarios:
              - user_message: "test"
                expected:
                  tool_calls: []
        """)
        path = tmp_path / "bad.yaml"
        path.write_text(yaml_content)
        with pytest.raises(ValueError, match="missing required"):
            load_scenarios(path)

    def test_load_missing_expected(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            scenarios:
              - id: bad
                user_message: "test"
        """)
        path = tmp_path / "bad.yaml"
        path.write_text(yaml_content)
        with pytest.raises(ValueError, match="missing 'expected'"):
            load_scenarios(path)


# ═══════════════════════════════════════════════════════════════════════════════
# Trajectory
# ═══════════════════════════════════════════════════════════════════════════════


class TestTrajectoryRecorder:
    def test_record_and_finalize(self) -> None:
        recorder = TrajectoryRecorder("test_scenario")
        recorder.record_tool_call("read_file", {"path": "test.txt"})
        recorder.record_tool_result("read_file", "Hello World")
        trajectory = recorder.finalize("Final answer", iterations=2, tools_used=["read_file"])

        assert trajectory.scenario_id == "test_scenario"
        assert trajectory.final_answer == "Final answer"
        assert trajectory.iterations == 2
        assert len(trajectory.steps) == 3  # tool_call + tool_result + final_answer
        assert trajectory.steps[0].step_type == "tool_call"
        assert trajectory.steps[1].step_type == "tool_result"
        assert trajectory.steps[2].step_type == "final_answer"

    def test_tool_calls_sequence(self) -> None:
        recorder = TrajectoryRecorder("seq_test")
        recorder.record_tool_call("list_files", {"path": "."})
        recorder.record_tool_result("list_files", "file1.txt\nfile2.txt")
        recorder.record_tool_call("read_file", {"path": "file1.txt"})
        recorder.record_tool_result("read_file", "content")
        trajectory = recorder.finalize("done")

        assert trajectory.tool_calls_sequence() == ["list_files", "read_file"]

    def test_to_dict_serializable(self) -> None:
        recorder = TrajectoryRecorder("dict_test")
        recorder.record_tool_call("test_tool", {"arg": "value"})
        trajectory = recorder.finalize("answer")
        d = trajectory.to_dict()

        assert d["scenario_id"] == "dict_test"
        assert d["final_answer"] == "answer"
        assert isinstance(d["steps"], list)
        assert d["steps"][0]["tool_name"] == "test_tool"

    def test_truncation_in_to_dict(self) -> None:
        recorder = TrajectoryRecorder("trunc_test")
        long_result = "x" * 1000
        recorder.record_tool_result("tool", long_result)
        trajectory = recorder.finalize("done")
        d = trajectory.to_dict()

        # Tool result should be truncated to 500 chars
        assert len(d["steps"][0]["tool_result"]) == 500


# ═══════════════════════════════════════════════════════════════════════════════
# Scorer
# ═══════════════════════════════════════════════════════════════════════════════


class TestCalibrationScorer:
    def _make_scenario(
        self,
        tool_calls: list[str] | None = None,
        contains: str | None = None,
        must_read: str | None = None,
        has_content: bool = True,
        category: str = "general",
    ) -> CalibrationScenario:
        return CalibrationScenario(
            id="test",
            user_message="test message",
            expected=ScenarioExpectation(
                tool_calls=tool_calls or [],
                contains=contains,
                must_read=must_read,
                has_content=has_content,
            ),
            category=category,
        )

    def _make_trajectory(
        self,
        tool_calls: list[tuple[str, dict[str, str]]] | None = None,
        final_answer: str = "Done.",
        status: str = "ok",
    ) -> Trajectory:
        steps: list[TrajectoryStep] = []
        for name, args in tool_calls or []:
            steps.append(TrajectoryStep(step_type="tool_call", tool_name=name, tool_args=args))
            steps.append(TrajectoryStep(step_type="tool_result", tool_name=name, tool_result="ok"))
        steps.append(TrajectoryStep(step_type="final_answer", content=final_answer))
        return Trajectory(
            scenario_id="test",
            steps=steps,
            final_answer=final_answer,
            status=status,
        )

    def test_exact_tool_match_passes(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario(tool_calls=["read_file"])
        trajectory = self._make_trajectory(tool_calls=[("read_file", {"path": "f.txt"})])
        result = scorer.score(scenario, trajectory)
        assert result.passed

    def test_subsequence_match_passes(self) -> None:
        """Extra tools are allowed as long as expected are in order."""
        scorer = CalibrationScorer()
        scenario = self._make_scenario(tool_calls=["read_file"])
        trajectory = self._make_trajectory(
            tool_calls=[("list_files", {"path": "."}), ("read_file", {"path": "f.txt"})]
        )
        result = scorer.score(scenario, trajectory)
        assert result.passed

    def test_missing_tool_fails(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario(tool_calls=["read_file", "edit_file"])
        trajectory = self._make_trajectory(tool_calls=[("read_file", {"path": "f.txt"})])
        result = scorer.score(scenario, trajectory)
        assert not result.passed
        assert "subsequence" in (result.failure_reason or "").lower()

    def test_no_tool_expected_but_called_in_no_tool_category(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario(tool_calls=[], category="no_tool")
        trajectory = self._make_trajectory(tool_calls=[("read_file", {"path": "x"})])
        result = scorer.score(scenario, trajectory)
        assert not result.passed

    def test_no_tool_expected_called_in_general_category(self) -> None:
        """In non-no_tool categories, extra tools don't cause failure."""
        scorer = CalibrationScorer()
        scenario = self._make_scenario(tool_calls=[], category="general")
        trajectory = self._make_trajectory(tool_calls=[("read_file", {"path": "x"})])
        result = scorer.score(scenario, trajectory)
        assert result.passed

    def test_contains_check_case_insensitive(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario(contains="hello")
        trajectory = self._make_trajectory(final_answer="HELLO World")
        result = scorer.score(scenario, trajectory)
        assert result.passed

    def test_contains_check_fails(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario(contains="105")
        trajectory = self._make_trajectory(final_answer="The answer is 42.")
        result = scorer.score(scenario, trajectory)
        assert not result.passed

    def test_must_read_passes(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario(tool_calls=["read_file"], must_read="data.csv")
        trajectory = self._make_trajectory(tool_calls=[("read_file", {"path": "data.csv"})])
        result = scorer.score(scenario, trajectory)
        assert result.passed

    def test_must_read_fails(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario(tool_calls=["read_file"], must_read="data.csv")
        trajectory = self._make_trajectory(tool_calls=[("read_file", {"path": "other.txt"})])
        result = scorer.score(scenario, trajectory)
        assert not result.passed

    def test_empty_answer_fails(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario(has_content=True)
        trajectory = self._make_trajectory(final_answer="   ")
        result = scorer.score(scenario, trajectory)
        assert not result.passed

    def test_error_status_fails(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario()
        trajectory = self._make_trajectory(status="error")
        result = scorer.score(scenario, trajectory)
        assert not result.passed

    def test_score_all(self) -> None:
        scorer = CalibrationScorer()
        s1 = self._make_scenario(category="tool_use")
        s2 = self._make_scenario(category="tool_use")
        s3 = self._make_scenario(category="no_tool")

        t1 = self._make_trajectory()
        t2 = self._make_trajectory(final_answer="   ")  # will fail has_content
        t3 = self._make_trajectory()

        results = [
            scorer.score(s1, t1),
            scorer.score(s2, t2),
            scorer.score(s3, t3),
        ]

        agg = scorer.score_all(results)
        assert agg.passed == 2
        assert agg.total == 3
        assert agg.by_category["tool_use"] == (1, 2)
        assert agg.by_category["no_tool"] == (1, 1)

    def test_is_subsequence(self) -> None:
        assert CalibrationScorer._is_subsequence(["a", "b"], ["a", "x", "b", "y"])
        assert CalibrationScorer._is_subsequence(["a"], ["a", "b"])
        assert not CalibrationScorer._is_subsequence(["b", "a"], ["a", "b"])
        assert CalibrationScorer._is_subsequence([], ["a", "b"])

    def test_scenario_result_to_dict(self) -> None:
        scorer = CalibrationScorer()
        scenario = self._make_scenario(tool_calls=["read_file"])
        trajectory = self._make_trajectory(tool_calls=[("read_file", {"path": "f"})])
        result = scorer.score(scenario, trajectory)
        d = result.to_dict()
        assert d["scenario_id"] == "test"
        assert d["passed"] is True
        assert "read_file" in d["expected_tools"]


# ═══════════════════════════════════════════════════════════════════════════════
# Config Editor
# ═══════════════════════════════════════════════════════════════════════════════


class TestConfigEditor:
    def test_apply_and_rollback(self, tmp_path: Path) -> None:
        editor = ConfigEditor(tmp_path)

        # Apply changes
        editor.apply(
            {
                "system_prompt": {"SOUL.md": "New soul content"},
                "few_shots": [{"user": "test", "assistant": {"content": "ok"}}],
                "tool_overrides": {"read_file": {"description": "New desc"}},
                "settings": {"max_steps": 25},
            }
        )

        # Verify files exist
        assert (tmp_path / "config" / "calibrated" / "bootstrap" / "SOUL.md").exists()
        assert (tmp_path / "config" / "calibrated" / "few_shots.yaml").exists()
        assert (tmp_path / "config" / "calibrated" / "tool_overrides.yaml").exists()
        assert (tmp_path / "config" / "calibrated" / "settings_override.yaml").exists()

        # Load back
        assert editor.load_few_shots() == [{"user": "test", "assistant": {"content": "ok"}}]
        overrides = editor.load_tool_overrides()
        assert "read_file" in overrides

        # Apply new changes (overwrites)
        editor.apply({"system_prompt": {"SOUL.md": "Updated soul"}})
        content = (tmp_path / "config" / "calibrated" / "bootstrap" / "SOUL.md").read_text()
        assert content == "Updated soul"

        # Rollback to previous
        editor.rollback()
        content = (tmp_path / "config" / "calibrated" / "bootstrap" / "SOUL.md").read_text()
        assert content == "New soul content"

    def test_reset(self, tmp_path: Path) -> None:
        editor = ConfigEditor(tmp_path)
        editor.apply({"few_shots": [{"user": "x", "assistant": {"content": "y"}}]})
        assert (tmp_path / "config" / "calibrated" / "few_shots.yaml").exists()
        editor.reset()
        assert not (tmp_path / "config" / "calibrated").exists()

    def test_metadata(self, tmp_path: Path) -> None:
        editor = ConfigEditor(tmp_path)
        editor.save_metadata("qwen2.5:7b", 85.0, 17, 20, 3)
        meta = editor.load_metadata()
        assert meta is not None
        assert meta["model_id"] == "qwen2.5:7b"
        assert meta["score_pct"] == 85.0
        assert meta["passed"] == 17
        assert meta["iterations"] == 3

    def test_load_without_files(self, tmp_path: Path) -> None:
        editor = ConfigEditor(tmp_path)
        assert editor.load_few_shots() == []
        assert editor.load_tool_overrides() == {}
        assert editor.load_metadata() is None

    def test_rollback_without_backup(self, tmp_path: Path) -> None:
        editor = ConfigEditor(tmp_path)
        # Should not raise
        editor.rollback()


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: ToolRegistry overrides
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolRegistryOverrides:
    def test_override_tool_description(self) -> None:
        from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

        class DummyTool(Tool):
            name = "dummy"
            description = "Original description"
            params = [ToolParam(name="arg", type="string", description="Original param")]
            risk_level = RiskLevel.LOW

            async def execute(self, **kwargs: object) -> str:
                return "ok"

        from corpclaw_lite.extensions.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(DummyTool())

        # Without override
        schemas = registry.to_schemas()
        assert schemas[0]["function"]["description"] == "Original description"
        assert (
            schemas[0]["function"]["parameters"]["properties"]["arg"]["description"]
            == "Original param"
        )

        # With override
        registry.load_overrides_dict(
            {
                "dummy": {
                    "description": "Better description",
                    "params": {"arg": {"description": "Better param desc"}},
                }
            }
        )
        schemas = registry.to_schemas()
        assert schemas[0]["function"]["description"] == "Better description"
        assert (
            schemas[0]["function"]["parameters"]["properties"]["arg"]["description"]
            == "Better param desc"
        )

    def test_load_overrides_from_yaml(self, tmp_path: Path) -> None:
        from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

        class DummyTool2(Tool):
            name = "dummy2"
            description = "Original"
            params = [ToolParam(name="x", type="string", description="original")]
            risk_level = RiskLevel.LOW

            async def execute(self, **kwargs: object) -> str:
                return "ok"

        from corpclaw_lite.extensions.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(DummyTool2())

        # Write YAML override
        override_path = tmp_path / "overrides.yaml"
        override_path.write_text(yaml.dump({"overrides": {"dummy2": {"description": "YAML desc"}}}))

        registry.load_overrides(override_path)
        schemas = registry.to_schemas()
        assert schemas[0]["function"]["description"] == "YAML desc"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: ContextBuilder few-shots
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextBuilderFewShots:
    def test_few_shots_injected_before_history(self) -> None:
        from corpclaw_lite.agent.context import ContextBuilder
        from corpclaw_lite.users.models import User

        user = User(id=1, telegram_id=1, name="Test", department="default")
        few_shots = [
            {"user": "Read x.txt", "assistant": {"content": "OK, reading x.txt"}},
        ]
        history = [
            {"role": "user", "content": "Previous question"},
            {"role": "assistant", "content": "Previous answer"},
        ]

        ctx = ContextBuilder.build_initial(
            user, "Current message", history=history, few_shots=few_shots
        )

        # Messages: few_shot user, few_shot assistant, history user, history assistant, current
        assert len(ctx.messages) == 5
        assert ctx.messages[0]["content"] == "Read x.txt"
        assert ctx.messages[1]["content"] == "OK, reading x.txt"
        assert ctx.messages[2]["content"] == "Previous question"
        assert ctx.messages[4]["content"] == "Current message"

    def test_few_shots_tool_calls(self) -> None:
        from corpclaw_lite.agent.context import ContextBuilder
        from corpclaw_lite.users.models import User

        user = User(id=1, telegram_id=1, name="Test", department="default")
        few_shots = [
            {
                "user": "Read file",
                "assistant": {
                    "tool_calls": [{"name": "read_file", "arguments": {"path": "f.txt"}}]
                },
            },
        ]

        ctx = ContextBuilder.build_initial(user, "msg", few_shots=few_shots)
        # Messages: few_shot user, few_shot assistant (tool desc), current msg
        assert len(ctx.messages) == 3
        assert "[Tool call:" in ctx.messages[1]["content"]

    def test_no_few_shots_unchanged(self) -> None:
        from corpclaw_lite.agent.context import ContextBuilder
        from corpclaw_lite.users.models import User

        user = User(id=1, telegram_id=1, name="Test", department="default")
        ctx = ContextBuilder.build_initial(user, "Hello")
        assert len(ctx.messages) == 1  # Just the user message


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: BootstrapLoader calibrated fallback
# ═══════════════════════════════════════════════════════════════════════════════


class TestBootstrapCalibrated:
    def test_calibrated_files_take_priority(self, tmp_path: Path) -> None:
        from corpclaw_lite.config.bootstrap import BootstrapLoader

        # Original bootstrap
        bootstrap_dir = tmp_path / "config" / "bootstrap"
        bootstrap_dir.mkdir(parents=True)
        (bootstrap_dir / "SOUL.md").write_text("Original soul")
        (bootstrap_dir / "BEHAVIOR.md").write_text("Original behavior")

        # Calibrated override (only SOUL.md)
        calibrated_dir = tmp_path / "config" / "calibrated" / "bootstrap"
        calibrated_dir.mkdir(parents=True)
        (calibrated_dir / "SOUL.md").write_text("Calibrated soul")

        loader = BootstrapLoader(bootstrap_dir)
        prompt = loader.get_system_prompt()

        assert "Calibrated soul" in prompt
        assert "Original behavior" in prompt
        assert "Original soul" not in prompt

    def test_no_calibrated_uses_original(self, tmp_path: Path) -> None:
        from corpclaw_lite.config.bootstrap import BootstrapLoader

        bootstrap_dir = tmp_path / "config" / "bootstrap"
        bootstrap_dir.mkdir(parents=True)
        (bootstrap_dir / "SOUL.md").write_text("Original soul")

        loader = BootstrapLoader(bootstrap_dir)
        prompt = loader.get_system_prompt()
        assert "Original soul" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# Analyzer
# ═══════════════════════════════════════════════════════════════════════════════


class _FakeAnalysisProvider:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_messages: list[dict[str, Any]] = []
        self.last_system: str | None = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        self.last_messages = messages
        self.last_system = system
        return LLMResponse(content=self.content)


class TestCalibrationAnalyzer:
    @pytest.mark.asyncio()
    async def test_analyze_accepts_fenced_json(self) -> None:
        provider = _FakeAnalysisProvider(
            '```json\n{"reasoning": "Need clearer examples.", "changes": {"few_shots": []}}\n```'
        )
        analyzer = CalibrationAnalyzer(provider)
        scenario = CalibrationScenario(
            id="failed",
            user_message="Read file",
            expected=ScenarioExpectation(tool_calls=["read_file"]),
        )
        trajectory = Trajectory(scenario_id="failed", final_answer="", status="error")
        failed = [ScenarioResult(scenario, trajectory, passed=False, failure_reason="bad")]

        result = await analyzer.analyze(
            model_id="local-model",
            failed=failed,
            passed=[],
            current_system_prompt="system",
            current_tool_schemas=[{"name": "read_file"}],
            current_few_shots=[],
            current_skills={"skill": "instructions"},
            current_subagent_prompts={"agent.md": "prompt"},
        )

        assert result["reasoning"] == "Need clearer examples."
        assert result["changes"] == {"few_shots": []}
        assert provider.last_system is not None
        assert "FAILED" in provider.last_messages[0]["content"]

    @pytest.mark.asyncio()
    async def test_analyze_rejects_json_without_changes(self) -> None:
        provider = _FakeAnalysisProvider('{"reasoning": "no changes"}')
        analyzer = CalibrationAnalyzer(provider)

        with pytest.raises(ValueError, match="missing 'changes'"):
            await analyzer.analyze(
                model_id="local-model",
                failed=[],
                passed=[],
                current_system_prompt="system",
                current_tool_schemas=[],
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════


class _FakeMemory:
    def __init__(self) -> None:
        self.cleared_keys: list[str] = []

    async def clear(self, key: str) -> None:
        self.cleared_keys.append(key)


class _FakeAgentLoop:
    def __init__(self, *, crash: bool = False) -> None:
        self.memory = _FakeMemory()
        self.crash = crash
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        *,
        user: User,
        message: str,
        system_prompt: str | None,
        trajectory_recorder: TrajectoryRecorder,
        few_shots: list[dict[str, Any]] | None = None,
    ) -> tuple[str, RunStats]:
        self.calls.append(
            {
                "user": user,
                "message": message,
                "system_prompt": system_prompt,
                "few_shots": few_shots,
            }
        )
        if self.crash:
            raise RuntimeError("agent crashed")
        trajectory_recorder.record_tool_call("read_file", {"path": "input.txt"})
        trajectory_recorder.record_tool_result("read_file", "ok")
        return (
            "Final answer",
            RunStats(iterations=2, tools_used=["read_file"], duration_ms=12.0),
        )


class _FakeSkill:
    id = "docs"


class _FakeSkillRegistry:
    def get_allowed_skills(self, user: User) -> list[_FakeSkill]:
        return [_FakeSkill()]


class _FakeSkillMatcher:
    def match(self, message: str, allowed_skills: list[_FakeSkill]) -> list[_FakeSkill]:
        return allowed_skills


class TestCalibrationRunner:
    @pytest.mark.asyncio()
    async def test_run_all_scores_cleans_workspace_and_clears_memory(self, tmp_path: Path) -> None:
        user = User(id=7, telegram_id=7, name="Cal", department="engineering")
        scenario = CalibrationScenario(
            id="read",
            user_message="Read input.txt",
            setup=ScenarioSetup(files=[("nested/input.txt", "hello")]),
            expected=ScenarioExpectation(tool_calls=["read_file"], has_content=True),
        )
        loop = _FakeAgentLoop()
        progress: list[tuple[str, bool, int, int]] = []

        runner = CalibrationRunner(
            loop,  # type: ignore[arg-type]
            user,
            "system prompt",
            tmp_path,
            few_shots=[{"user": "x", "assistant": {"content": "y"}}],
            skill_matcher=_FakeSkillMatcher(),  # type: ignore[arg-type]
            skill_registry=_FakeSkillRegistry(),  # type: ignore[arg-type]
        )
        results = await runner.run_all(
            [scenario],
            on_progress=lambda scenario_id, passed, index, total: progress.append(
                (scenario_id, passed, index, total)
            ),
        )

        assert results[0].passed
        assert progress == [("read", True, 1, 1)]
        assert not (tmp_path / "nested" / "input.txt").exists()
        assert loop.memory.cleared_keys == ["7"]
        assert loop.calls[0]["few_shots"] == [{"user": "x", "assistant": {"content": "y"}}]

    @pytest.mark.asyncio()
    async def test_run_all_records_crashes_as_failed_results(self, tmp_path: Path) -> None:
        user = User(id=8, telegram_id=8, name="Cal", department="engineering")
        scenario = CalibrationScenario(
            id="crash",
            user_message="Do work",
            expected=ScenarioExpectation(has_content=True),
        )
        loop = _FakeAgentLoop(crash=True)
        runner = CalibrationRunner(loop, user, "system prompt", tmp_path)  # type: ignore[arg-type]

        results = await runner.run_all([scenario])

        assert not results[0].passed
        assert "Scenario crashed" in (results[0].failure_reason or "")
        assert loop.memory.cleared_keys == ["8"]


# ═══════════════════════════════════════════════════════════════════════════════
# Loop
# ═══════════════════════════════════════════════════════════════════════════════


class _FakeBootstrapLoader:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get_system_prompt(self) -> str:
        return "fake system prompt"


class _FakeToolRegistry:
    def __init__(self) -> None:
        self.loaded_overrides: list[dict[str, Any]] = []

    def to_schemas(self) -> list[dict[str, Any]]:
        return [{"function": {"name": "read_file"}}]

    def load_overrides_dict(self, overrides: dict[str, Any]) -> None:
        self.loaded_overrides.append(overrides)


def _write_loop_scenarios(path: Path) -> CalibrationScenario:
    path.write_text(
        textwrap.dedent("""\
            scenarios:
              - id: read
                user_message: "Read input.txt"
                category: tool_use
                expected:
                  tool_calls: ["read_file"]
                  has_content: true
        """),
        encoding="utf-8",
    )
    return CalibrationScenario(
        id="read",
        user_message="Read input.txt",
        expected=ScenarioExpectation(tool_calls=["read_file"], has_content=True),
        category="tool_use",
    )


def _loop_result(scenario: CalibrationScenario, *, passed: bool) -> ScenarioResult:
    steps = (
        [
            TrajectoryStep(
                step_type="tool_call", tool_name="read_file", tool_args={"path": "input.txt"}
            )
        ]
        if passed
        else []
    )
    trajectory = Trajectory(
        scenario_id=scenario.id,
        steps=steps,
        final_answer="done" if passed else "",
        status="ok" if passed else "error",
    )
    return ScenarioResult(
        scenario=scenario,
        trajectory=trajectory,
        passed=passed,
        failure_reason=None if passed else "failed",
    )


def _patch_loop_basics(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    stacks: list[SimpleNamespace],
) -> None:
    monkeypatch.setattr("corpclaw_lite.config.loader.load_settings", lambda path: settings)
    monkeypatch.setattr(
        "corpclaw_lite.agent.factory.build_agent_stack",
        lambda settings: stacks.pop(0),
    )
    monkeypatch.setattr("corpclaw_lite.config.bootstrap.BootstrapLoader", _FakeBootstrapLoader)


class TestCalibrationLoop:
    @pytest.mark.asyncio()
    async def test_run_dry_run_returns_baseline_and_removes_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scenario = _write_loop_scenarios(tmp_path / "scenarios.yaml")
        settings = Settings(
            llm=LLMSettings(routing=[RoutingRule(task_kind="default", model="local-model")])
        )
        stack = SimpleNamespace(
            loop=object(),
            tool_registry=_FakeToolRegistry(),
            skill_matcher=None,
            skill_registry=None,
        )
        _patch_loop_basics(monkeypatch, settings, [stack])

        class FakeRunner:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def run_all(self, scenarios: list[CalibrationScenario]) -> list[ScenarioResult]:
                return [_loop_result(scenario, passed=True)]

        monkeypatch.setattr("corpclaw_lite.calibration.loop.CalibrationRunner", FakeRunner)

        report = await CalibrationLoop(
            "local",
            "cloud",
            tmp_path / "scenarios.yaml",
            tmp_path,
            dry_run=True,
        ).run()

        assert report.model_id == "local-model"
        assert report.baseline_passed == 1
        assert report.final_passed == 1
        assert report.iterations_run == 0
        assert not (tmp_path / ".calibration_workspace").exists()

    @pytest.mark.asyncio()
    async def test_run_returns_score_only_when_cloud_provider_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scenario = _write_loop_scenarios(tmp_path / "scenarios.yaml")
        settings = Settings(
            llm=LLMSettings(routing=[RoutingRule(task_kind="default", model="local-model")])
        )
        stack = SimpleNamespace(
            loop=object(),
            tool_registry=_FakeToolRegistry(),
            skill_matcher=None,
            skill_registry=None,
        )
        _patch_loop_basics(monkeypatch, settings, [stack])

        class FakeRunner:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def run_all(self, scenarios: list[CalibrationScenario]) -> list[ScenarioResult]:
                return [_loop_result(scenario, passed=False)]

        class FakeProviderRegistry:
            def get(self, name: str) -> object | None:
                return None

        from corpclaw_lite.config.providers import ProviderRegistry

        monkeypatch.setattr("corpclaw_lite.calibration.loop.CalibrationRunner", FakeRunner)
        monkeypatch.setattr(
            ProviderRegistry,
            "from_env",
            classmethod(lambda cls: FakeProviderRegistry()),
        )

        report = await CalibrationLoop(
            "local",
            "missing-cloud",
            tmp_path / "scenarios.yaml",
            tmp_path,
            dry_run=False,
        ).run()

        assert report.baseline_passed == 0
        assert report.final_passed == 0
        assert report.iterations_run == 0
        assert report.improvements == ["Cloud provider not available — dry-run only"]
        assert not (tmp_path / ".calibration_workspace").exists()

    @pytest.mark.asyncio()
    async def test_run_keeps_iteration_when_score_improves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scenario = _write_loop_scenarios(tmp_path / "scenarios.yaml")
        settings = Settings(
            llm=LLMSettings(
                routing=[
                    RoutingRule(task_kind="default", provider="local", model="local-model"),
                    RoutingRule(task_kind="calibration", provider="cloud", model="cloud-model"),
                ]
            )
        )
        baseline_registry = _FakeToolRegistry()
        improved_registry = _FakeToolRegistry()
        stacks = [
            SimpleNamespace(
                loop=object(),
                tool_registry=baseline_registry,
                skill_matcher=None,
                skill_registry=None,
            ),
            SimpleNamespace(
                loop=object(),
                tool_registry=improved_registry,
                skill_matcher=None,
                skill_registry=None,
            ),
        ]
        _patch_loop_basics(monkeypatch, settings, stacks)

        class FakeRunner:
            queued = [[_loop_result(scenario, passed=False)], [_loop_result(scenario, passed=True)]]

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def run_all(self, scenarios: list[CalibrationScenario]) -> list[ScenarioResult]:
                return self.queued.pop(0)

        class FakeProviderRegistry:
            def get(self, name: str) -> object | None:
                return object()

        class FakeRouter:
            def for_task(self, task_kind: str) -> object:
                return object()

            def has_task_route(self, task_kind: str) -> bool:
                return True

        class FakeAnalyzer:
            def __init__(self, provider: object) -> None:
                self.provider = provider

            async def analyze(self, **kwargs: Any) -> dict[str, Any]:
                return {
                    "reasoning": "Add a concrete read_file example",
                    "changes": {
                        "tool_overrides": {"read_file": {"description": "Read a file"}},
                        "few_shots": [{"user": "Read x", "assistant": {"content": "Done"}}],
                    },
                }

        from corpclaw_lite.config.providers import ProviderRegistry
        from corpclaw_lite.llm.router import LLMRouter

        monkeypatch.setattr("corpclaw_lite.calibration.loop.CalibrationRunner", FakeRunner)
        monkeypatch.setattr("corpclaw_lite.calibration.loop.CalibrationAnalyzer", FakeAnalyzer)
        monkeypatch.setattr(
            ProviderRegistry,
            "from_env",
            classmethod(lambda cls: FakeProviderRegistry()),
        )
        monkeypatch.setattr(
            LLMRouter,
            "from_settings",
            classmethod(lambda cls, llm_settings, provider_registry: FakeRouter()),
        )

        report = await CalibrationLoop(
            "local",
            "cloud",
            tmp_path / "scenarios.yaml",
            tmp_path,
            max_iterations=1,
            dry_run=False,
        ).run()

        assert report.baseline_passed == 0
        assert report.final_passed == 1
        assert report.iterations_run == 1
        assert report.improvements == ["Iter 1: +1 — Add a concrete read_file example"]
        assert improved_registry.loaded_overrides == [{"read_file": {"description": "Read a file"}}]
        assert (tmp_path / "config" / "calibrated" / "metadata.yaml").exists()
