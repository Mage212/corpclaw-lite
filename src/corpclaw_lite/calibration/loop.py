"""Calibration loop — orchestrate the full hill-climbing calibration cycle."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corpclaw_lite.calibration.analyzer import CalibrationAnalyzer
from corpclaw_lite.calibration.editor import ConfigEditor
from corpclaw_lite.calibration.runner import CalibrationRunner
from corpclaw_lite.calibration.scenarios import load_scenarios
from corpclaw_lite.calibration.scorer import CalibrationScore, CalibrationScorer
from corpclaw_lite.config.settings import Settings

__all__ = [
    "CalibrationLoop",
    "CalibrationReport",
]

logger = logging.getLogger(__name__)


@dataclass
class CalibrationReport:
    """Summary of a calibration run."""

    model_id: str
    baseline_passed: int
    baseline_total: int
    final_passed: int
    final_total: int
    iterations_run: int
    improvements: list[str] = field(default_factory=lambda: list[str]())

    @property
    def baseline_pct(self) -> float:
        """Baseline pass rate."""
        return (self.baseline_passed / self.baseline_total * 100) if self.baseline_total else 0.0

    @property
    def final_pct(self) -> float:
        """Final pass rate."""
        return (self.final_passed / self.final_total * 100) if self.final_total else 0.0


class CalibrationLoop:
    """Orchestrates the full calibration cycle.

    1. Run baseline scenarios with the local model
    2. For each iteration:
       a. Collect failures
       b. Send to cloud model for analysis
       c. Apply proposed config changes
       d. Re-run scenarios with updated config
       e. Keep changes if score improved, else rollback
    3. Save final calibration metadata
    """

    def __init__(
        self,
        local_provider_name: str,
        cloud_provider_name: str,
        scenarios_path: Path,
        project_root: Path,
        max_iterations: int = 5,
        dry_run: bool = False,
    ) -> None:
        self._local_name = local_provider_name
        self._cloud_name = cloud_provider_name
        self._scenarios_path = scenarios_path
        self._project_root = project_root
        self._max_iterations = max_iterations
        self._dry_run = dry_run

    async def run(self) -> CalibrationReport:
        """Run the full calibration loop.

        Returns:
            CalibrationReport summarising the calibration outcome.
        """
        from corpclaw_lite.agent.factory import build_agent_stack
        from corpclaw_lite.config.loader import load_settings

        # Load scenarios
        scenarios = load_scenarios(self._scenarios_path)
        print(f"Loaded {len(scenarios)} calibration scenarios")

        editor = ConfigEditor(self._project_root)
        scorer = CalibrationScorer()

        # Build agent stack with current config
        settings = load_settings(self._project_root / "config" / "settings.yaml")
        _cal_stack = build_agent_stack(settings)
        agent_loop = _cal_stack.loop
        registry = _cal_stack.tool_registry

        # Create a calibration user
        from corpclaw_lite.users.models import User

        # Use "engineering" department so the calibration user has access to ALL tools
        # (allowed_tools: ["*"]). With "default", to_schemas_for_user() would hide
        # write_file, edit_file, exec_script — breaking scenarios that test those tools.
        cal_user = User(
            id=0,
            telegram_id=0,
            name="Calibration Runner",
            department="engineering",
        )

        # Get system prompt
        from corpclaw_lite.config.bootstrap import BootstrapLoader

        bootstrap = BootstrapLoader(self._project_root / "config" / "bootstrap")
        system_prompt = bootstrap.get_system_prompt() or ""

        # Prepare workspace
        workspace = self._project_root / ".calibration_workspace"
        workspace.mkdir(exist_ok=True)

        # Baseline run
        runner = CalibrationRunner(
            agent_loop,
            cal_user,
            system_prompt,
            workspace,
            skill_matcher=_cal_stack.skill_matcher,
            skill_registry=_cal_stack.skill_registry,
        )
        print(f"\nRunning baseline ({len(scenarios)} scenarios)...")
        baseline_results = await runner.run_all(scenarios)
        baseline_score = scorer.score_all(baseline_results)

        print(
            f"Baseline: {baseline_score.passed}/{baseline_score.total} ({baseline_score.pct:.0f}%)"
        )
        self._print_category_scores(baseline_score)

        if self._dry_run:
            # Clean up and return
            if workspace.exists():
                shutil.rmtree(workspace)
            return CalibrationReport(
                model_id=self._get_model_id(settings),
                baseline_passed=baseline_score.passed,
                baseline_total=baseline_score.total,
                final_passed=baseline_score.passed,
                final_total=baseline_score.total,
                iterations_run=0,
            )

        # Get cloud provider for analysis via router
        from corpclaw_lite.config.providers import ProviderRegistry
        from corpclaw_lite.llm.router import LLMRouter

        provider_registry = ProviderRegistry.from_env()
        cloud_conn = provider_registry.get(self._cloud_name)
        if not cloud_conn:
            print(f"\n⚠️  Cloud provider '{self._cloud_name}' not registered in .env")
            print("   Register it via PROVIDER_* env vars or use --dry-run for score-only mode")
            if workspace.exists():
                shutil.rmtree(workspace)
            return CalibrationReport(
                model_id=self._get_model_id(settings),
                baseline_passed=baseline_score.passed,
                baseline_total=baseline_score.total,
                final_passed=baseline_score.passed,
                final_total=baseline_score.total,
                iterations_run=0,
                improvements=["Cloud provider not available — dry-run only"],
            )

        # Build a temporary router to resolve calibration-specific provider
        router = LLMRouter.from_settings(settings.llm, provider_registry)
        calibration_provider = router.for_task("calibration")

        # If no calibration rule, try building directly from cloud provider connection
        if not router.has_task_route("calibration"):
            from corpclaw_lite.llm.router import build_provider

            cloud_model: str = "calibration"
            for rule in settings.llm.routing:
                if rule.task_kind == "calibration" and rule.provider == self._cloud_name:
                    if rule.model is not None:
                        cloud_model = rule.model
                    break

            built = build_provider(cloud_conn, model=cloud_model)
            if built is None:
                print(
                    f"\n⚠️  Cloud provider '{self._cloud_name}' could not be built"
                    " (missing API key?)"
                )
                if workspace.exists():
                    shutil.rmtree(workspace)
                return CalibrationReport(
                    model_id=self._get_model_id(settings),
                    baseline_passed=baseline_score.passed,
                    baseline_total=baseline_score.total,
                    final_passed=baseline_score.passed,
                    final_total=baseline_score.total,
                    iterations_run=0,
                    improvements=["Cloud provider not available"],
                )
            calibration_provider = built

        analyzer = CalibrationAnalyzer(calibration_provider)

        # Hill-climbing loop
        best_score = baseline_score
        best_results = baseline_results
        improvements: list[str] = []
        iteration = 0

        for iteration in range(1, self._max_iterations + 1):
            failed = [r for r in best_results if not r.passed]
            if not failed:
                print("\n🎉 All scenarios passed!")
                break

            print(f"\n── Iteration {iteration}/{self._max_iterations} ({len(failed)} failures) ──")

            # Analyse failures — collect all current config surfaces for context
            current_skills = self._load_current_skills()
            current_subagent_prompts = self._load_current_subagent_prompts()

            try:
                proposed = await analyzer.analyze(
                    model_id=self._get_model_id(settings),
                    failed=failed,
                    passed=[r for r in best_results if r.passed],
                    current_system_prompt=system_prompt,
                    current_tool_schemas=registry.to_schemas(),
                    current_few_shots=editor.load_few_shots(),
                    current_skills=current_skills,
                    current_subagent_prompts=current_subagent_prompts,
                )
            except Exception as e:
                print(f"  ⚠️  Cloud analysis failed: {e}")
                continue

            reasoning = proposed.get("reasoning", "—")
            print(f"  Analysis: {reasoning}")

            # Apply changes
            editor.apply(proposed["changes"])

            # Rebuild stack with new config
            new_settings = load_settings(self._project_root / "config" / "settings.yaml")
            _new_stack = build_agent_stack(new_settings)
            new_loop = _new_stack.loop
            new_registry = _new_stack.tool_registry

            # Reload bootstrap with calibrated overrides
            new_bootstrap = BootstrapLoader(self._project_root / "config" / "bootstrap")
            new_system_prompt = new_bootstrap.get_system_prompt() or ""

            # Load and apply tool overrides
            overrides = editor.load_tool_overrides()
            if overrides:
                new_registry.load_overrides_dict(overrides)

            # Load calibrated few-shots to inject during re-evaluation
            calibrated_few_shots = editor.load_few_shots() or None

            # Re-run with updated config (including few-shots)
            new_runner = CalibrationRunner(
                new_loop,
                cal_user,
                new_system_prompt,
                workspace,
                few_shots=calibrated_few_shots,
                skill_matcher=_new_stack.skill_matcher,
                skill_registry=_new_stack.skill_registry,
            )
            new_results = await new_runner.run_all(scenarios)
            new_score = scorer.score_all(new_results)

            # Keep or discard
            if new_score.passed > best_score.passed:
                delta = new_score.passed - best_score.passed
                print(
                    f"  ✅ KEEP: {new_score.passed}/{new_score.total} (+{delta}) — {reasoning[:80]}"
                )
                best_score = new_score
                best_results = new_results
                system_prompt = new_system_prompt
                improvements.append(f"Iter {iteration}: +{delta} — {reasoning[:100]}")
            elif new_score.passed == best_score.passed:
                print(f"  ⏸️  DISCARD (no improvement): {new_score.passed}/{new_score.total}")
                editor.rollback()
            else:
                regression = best_score.passed - new_score.passed
                print(
                    f"  ❌ DISCARD (regression -{regression}): {new_score.passed}/{new_score.total}"
                )
                editor.rollback()

        # Save metadata
        model_id = self._get_model_id(settings)
        editor.save_metadata(
            model_id=model_id,
            score=best_score.pct,
            passed=best_score.passed,
            total=best_score.total,
            iterations=iteration,
        )

        # Cleanup workspace
        if workspace.exists():
            shutil.rmtree(workspace)

        print(f"\n{'═' * 50}")
        print(f"Calibration complete for {model_id}")
        print(
            f"  Baseline: {baseline_score.passed}/{baseline_score.total} "
            f"({baseline_score.pct:.0f}%)"
        )
        print(f"  Final:    {best_score.passed}/{best_score.total} ({best_score.pct:.0f}%)")
        if improvements:
            print("  Improvements:")
            for imp in improvements:
                print(f"    • {imp}")
        print("  Config saved to: config/calibrated/")

        return CalibrationReport(
            model_id=model_id,
            baseline_passed=baseline_score.passed,
            baseline_total=baseline_score.total,
            final_passed=best_score.passed,
            final_total=best_score.total,
            iterations_run=iteration,
            improvements=improvements,
        )

    def _load_current_skills(self) -> dict[str, str]:
        """Load current skill instructions for all skills in the skills/ directory.

        Returns {skill_id: instructions_text}. Calibrated overrides are already
        applied by SkillLoader, so this captures what the model actually sees.
        """
        skills_dir = self._project_root / "skills"
        if not skills_dir.exists():
            return {}

        from corpclaw_lite.extensions.skills.loader import SkillLoader

        result: dict[str, str] = {}
        for md_file in skills_dir.glob("*.md"):
            skill = SkillLoader.load_from_file(md_file)
            if skill:
                result[skill.id] = skill.instructions
        return result

    def _load_current_subagent_prompts(self) -> dict[str, str]:
        """Load current system prompts for all subagents in config/subagents/.

        Returns {filename: prompt_text}. Checks calibrated overrides first,
        matching the same priority as SubagentDispatcher.
        """
        subagents_dir = self._project_root / "config" / "subagents"
        if not subagents_dir.exists():
            return {}

        import yaml

        result: dict[str, str] = {}
        for yaml_file in subagents_dir.glob("*.yaml"):
            try:
                data: dict[str, Any] = yaml.safe_load(yaml_file.read_text(encoding="utf-8")) or {}
                prompt_path_str = str(data.get("prompt_path", ""))
                if not prompt_path_str:
                    continue

                # Anchor to project_root, same as how the rest of the app resolves paths
                prompt_file = self._project_root / prompt_path_str
                filename = Path(prompt_path_str).name

                # Calibrated override takes priority (mirrors SubagentDispatcher logic)
                calibrated = (
                    self._project_root
                    / "config"
                    / "calibrated"
                    / "bootstrap"
                    / "subagents"
                    / filename
                )
                if calibrated.exists():
                    result[filename] = calibrated.read_text(encoding="utf-8")
                elif prompt_file.exists():
                    result[filename] = prompt_file.read_text(encoding="utf-8")
            except Exception:
                pass
        return result

    @staticmethod
    def _get_model_id(settings: Settings) -> str:
        """Extract model identifier from default routing rule."""
        for rule in settings.llm.routing:
            if rule.task_kind == "default" and rule.model:
                return rule.model
        # Fallback: first rule with a model
        for rule in settings.llm.routing:
            if rule.model:
                return rule.model
        return "unknown"

    @staticmethod
    def _print_category_scores(score: CalibrationScore) -> None:
        """Print per-category breakdown."""
        if not score.by_category:
            return
        for cat, (cat_passed, cat_total) in sorted(score.by_category.items()):
            pct = cat_passed / cat_total * 100 if cat_total else 0
            status = "✅" if cat_passed == cat_total else "⚠️"
            print(f"  {status} {cat}: {cat_passed}/{cat_total} ({pct:.0f}%)")
