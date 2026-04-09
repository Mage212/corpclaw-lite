"""Tests for calibration scenarios, scorer, trajectory, and editor."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from corpclaw_lite.calibration.editor import ConfigEditor
from corpclaw_lite.calibration.scenarios import (
    CalibrationScenario,
    ScenarioExpectation,
    load_scenarios,
)
from corpclaw_lite.calibration.scorer import CalibrationScorer
from corpclaw_lite.calibration.trajectory import Trajectory, TrajectoryRecorder, TrajectoryStep

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
