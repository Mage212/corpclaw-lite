"""Calibration loop — orchestrate the full hill-climbing calibration cycle."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

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

        cal_user = User(
            id=0,
            telegram_id=0,
            name="Calibration Runner",
            department="default",
        )

        # Get system prompt
        from corpclaw_lite.config.bootstrap import BootstrapLoader

        bootstrap = BootstrapLoader(self._project_root / "config" / "bootstrap")
        system_prompt = bootstrap.get_system_prompt() or ""

        # Prepare workspace
        workspace = self._project_root / ".calibration_workspace"
        workspace.mkdir(exist_ok=True)

        # Baseline run
        runner = CalibrationRunner(agent_loop, cal_user, system_prompt, workspace)
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

        # Get cloud provider for analysis
        from corpclaw_lite.llm.router import build_provider

        cloud_spec = settings.llm.named.get(self._cloud_name)
        if not cloud_spec:
            print(f"\n⚠️  Cloud provider '{self._cloud_name}' not configured in settings.yaml")
            print("   Add it to llm.named or use --dry-run for score-only mode")
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

        cloud_provider = build_provider(cloud_spec)
        if cloud_provider is None:
            print(f"\n⚠️  Cloud provider '{self._cloud_name}' could not be built (missing API key?)")
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

        analyzer = CalibrationAnalyzer(cloud_provider)

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

            # Analyse failures
            try:
                proposed = await analyzer.analyze(
                    model_id=self._get_model_id(settings),
                    failed=failed,
                    passed=[r for r in best_results if r.passed],
                    current_system_prompt=system_prompt,
                    current_tool_schemas=registry.to_schemas(),
                    current_few_shots=editor.load_few_shots(),
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

            # Re-run
            new_runner = CalibrationRunner(new_loop, cal_user, new_system_prompt, workspace)
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

    @staticmethod
    def _get_model_id(settings: Settings) -> str:
        """Extract model identifier from settings."""
        default_name = settings.llm.default
        provider = settings.llm.named.get(default_name)
        if provider:
            return str(provider.model)
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
