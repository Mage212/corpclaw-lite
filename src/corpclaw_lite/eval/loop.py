"""Eval orchestration loop — A/B passes with Phase 0 guards on/off (B-060, step 6).

Builds an AgentStack, runs the corpus twice (guards enabled, then disabled),
and produces an :class:`~corpclaw_lite.eval.report.ABReport` answering the
central B-060 question: did the Phase 0 guards (B-055 dedup, B-056 planning-text)
improve outcomes on the local model?

The single-pass mode (``ab_guards=False``) runs once with guards on and emits a
:class:`~corpclaw_lite.eval.report.PassReport` — useful when the cloud judge is
unavailable and a quick deterministic-only snapshot is wanted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpclaw_lite.agent.guards import (
    PlanningTextGuardConfig,
    ResultDedupGuardConfig,
)
from corpclaw_lite.eval.report import ABReport, MultiSeedReport, PassReport
from corpclaw_lite.eval.runner import EvalRunner
from corpclaw_lite.eval.scenarios import load_scenarios
from corpclaw_lite.paths import PROJECT_ROOT

__all__ = ["EvalLoop"]

if TYPE_CHECKING:
    from corpclaw_lite.eval.judge import LLMJudge

logger = logging.getLogger(__name__)


class EvalLoop:
    """Orchestrate one or two eval passes over the corpus.

    Args:
        scenarios_path: YAML corpus path (default config/eval_scenarios.yaml).
        judge: optional cloud LLM judge. When None, non-settling turns get the
            conservative fallback (deterministic-only scoring).
        corpus_dir: directory with binary fixtures referenced by copy_from_corpus.
        output_dir: where to write JSON/Markdown reports.
        ab_guards: when True (default), run two passes (guards on/off) and
            compare. When False, run one pass with guards on.
        settings_path: path to settings.yaml (default config/settings.yaml).
        workspace_base: parent dir for the per-pass eval workspace.
        seeds: number of A/B seed runs for median aggregation (D-052). Default 1
            (single seed, backward-compatible). Seeds > 1 only take effect in
            A/B mode; in single-pass mode they are ignored. Each seed is a full
            A/B (2 passes) with isolated workspace and memory; the aggregation
            takes the per-scenario median to filter sampling noise.
    """

    def __init__(
        self,
        scenarios_path: Path | str | None = None,
        judge: LLMJudge | None = None,
        corpus_dir: Path | str | None = None,
        output_dir: Path | str | None = None,
        ab_guards: bool = True,
        settings_path: Path | str | None = None,
        workspace_base: Path | str | None = None,
        on_scenario_progress: Callable[[str, bool, int, int], None] | None = None,
        seeds: int = 1,
    ) -> None:
        self._scenarios_path = (
            Path(scenarios_path)
            if scenarios_path
            else (PROJECT_ROOT / "config" / "eval_scenarios.yaml")
        )
        self._judge = judge
        self._corpus_dir = Path(corpus_dir) if corpus_dir else None
        self._output_dir = Path(output_dir) if output_dir else (PROJECT_ROOT / "reports" / "eval")
        self._ab_guards = ab_guards
        self._settings_path = (
            Path(settings_path) if settings_path else (PROJECT_ROOT / "config" / "settings.yaml")
        )
        self._workspace_base = (
            Path(workspace_base) if workspace_base else (PROJECT_ROOT / ".eval_workspace")
        )
        self._on_progress = on_scenario_progress
        self._seeds = max(1, seeds)

    async def run(self) -> ABReport | PassReport | MultiSeedReport:
        """Run the eval and return the report.

        Returns a :class:`MultiSeedReport` in multi-seed A/B mode (seeds > 1),
        an :class:`ABReport` in single-seed A/B mode, otherwise a
        :class:`PassReport`.
        """
        scenarios = load_scenarios(self._scenarios_path)
        logger.info("[eval] Loaded %d scenarios from %s", len(scenarios), self._scenarios_path)

        # Multi-seed A/B runs each seed as a full on/off pair (no upfront pass).
        if self._ab_guards and self._seeds > 1:
            return await self._run_multi_seed(scenarios)

        # Single-pass mode (guards on only).
        if not self._ab_guards:
            on_report = await self._run_pass(scenarios, guards_enabled=True, label="guards_on")
            self._print_pass(on_report)
            _write_pass(on_report, self._output_dir)
            return on_report

        # Single-seed A/B.
        on_report = await self._run_pass(scenarios, guards_enabled=True, label="guards_on")
        self._print_pass(on_report)
        off_report = await self._run_pass(scenarios, guards_enabled=False, label="guards_off")
        self._print_pass(off_report)

        ab = ABReport.compare(on_report, off_report)
        ab.write(self._output_dir)
        self._print_verdict(ab)
        return ab

    async def _run_single_ab(
        self,
        scenarios: list[Any],
        *,
        output_override: Path | None = None,
        workspace_override: Path | None = None,
    ) -> ABReport:
        """Run one guards-on vs guards-off A/B comparison.

        ``output_override`` redirects the ABReport JSON/MD to a subdirectory
        (used by multi-seed to keep per-seed reports). ``workspace_override``
        isolates the per-seed workspace so seeds do not collide on files/memory.
        """
        saved_workspace = self._workspace_base
        if workspace_override is not None:
            self._workspace_base = workspace_override
        try:
            on_report = await self._run_pass(scenarios, guards_enabled=True, label="guards_on")
            self._print_pass(on_report)
            off_report = await self._run_pass(scenarios, guards_enabled=False, label="guards_off")
            self._print_pass(off_report)
        finally:
            self._workspace_base = saved_workspace

        ab = ABReport.compare(on_report, off_report)
        if output_override is not None:
            ab.write(output_override)
        return ab

    async def _run_multi_seed(self, scenarios: list[Any]) -> MultiSeedReport:
        """Run N A/B seeds, aggregate per-scenario medians (D-052)."""
        ab_reports: list[ABReport] = []
        for n in range(1, self._seeds + 1):
            print(f"\n═══ Seed {n}/{self._seeds} ═══")
            logger.info("[eval] Starting seed %d/%d", n, self._seeds)
            seed_dir = self._output_dir / f"seed_{n}"
            seed_workspace = self._workspace_base / f"seed_{n}"
            ab = await self._run_single_ab(
                scenarios,
                output_override=seed_dir,
                workspace_override=seed_workspace,
            )
            ab_reports.append(ab)

        ms = MultiSeedReport.from_ab_reports(ab_reports)
        ms.write(self._output_dir)
        self._print_multiseed_verdict(ms)
        return ms

    # ──────────────────────────── one pass ───────────────────────────────

    async def _run_pass(
        self,
        scenarios: list[Any],
        guards_enabled: bool,
        label: str,
    ) -> PassReport:
        """Build a stack with the requested guard state and run the corpus."""
        from corpclaw_lite.agent.factory import build_agent_stack
        from corpclaw_lite.config.bootstrap import BootstrapLoader
        from corpclaw_lite.config.loader import load_settings
        from corpclaw_lite.users.models import User

        settings = load_settings(self._settings_path)
        if not guards_enabled:
            settings.agent.result_dedup_guard = ResultDedupGuardConfig(enabled=False)
            settings.agent.planning_text_guard = PlanningTextGuardConfig(enabled=False)

        stack = build_agent_stack(settings)
        agent_loop = stack.loop

        # Enrich the judge with the main agent's actual tool surface so it does
        # not penalise delegation (dispatch_subagent) for execution tools the
        # agent does not have directly (D-028 router+executor pattern).
        if self._judge is not None and getattr(self._judge, "_agent_tools", None) is None:
            self._judge._agent_tools = list(stack.tool_registry.items().keys())  # type: ignore[attr-defined]

        # engineering department → allowed_tools ["*"] (same rationale as calibration).
        eval_user = User(
            id=0,
            telegram_id=0,
            name="Eval Runner",
            department="engineering",
        )
        bootstrap = BootstrapLoader(PROJECT_ROOT / "config" / "bootstrap")
        system_prompt = bootstrap.get_system_prompt() or ""

        workspace = self._workspace_base / label
        workspace.mkdir(parents=True, exist_ok=True)
        # Clear any leftover files from a previous run of this pass.
        for child in workspace.iterdir():
            if child.is_file():
                child.unlink()

        runner = EvalRunner(
            agent_loop=agent_loop,
            user=eval_user,
            system_prompt=system_prompt,
            workspace_dir=workspace,
            corpus_dir=self._corpus_dir,
            few_shots=stack.few_shots,
            judge=self._judge,
        )
        logger.info("[eval] Starting pass '%s' (guards=%s)", label, guards_enabled)
        scores = await runner.run_all(scenarios, on_progress=self._on_progress)
        return PassReport(label=label, scenario_scores=scores)

    # ─────────────────────────── reporting ───────────────────────────────

    def _print_pass(self, report: PassReport) -> None:
        print(
            f"[{report.label}] {report.passed}/{report.total} passed "
            f"({report.pass_rate:.0%}), mean overall {report.mean_overall:.2f}, "
            f"mean correctness {report.mean_correctness:.2f}"
        )

    def _print_verdict(self, ab: ABReport) -> None:
        labels = {
            "guards_help": "✅ Phase 0 guards HELPED",
            "guards_hurt": "⚠️ Phase 0 guards HURT",
            "guards_neutral": "➖ Phase 0 guards made no significant difference",
        }
        print()
        print(f"{labels[ab.verdict]}")
        print(
            f"  pass rate: {ab.guards_on.pass_rate:.0%} (on) vs "
            f"{ab.guards_off.pass_rate:.0%} (off) → {ab.pass_rate_delta:+.0%}"
        )
        print(
            f"  mean overall: {ab.guards_on.mean_overall:.2f} (on) vs "
            f"{ab.guards_off.mean_overall:.2f} (off) → {ab.mean_overall_delta:+.2f}"
        )
        print(f"  improved: {ab.improved_count}, regressed: {ab.regressed_count}")
        print(f"  reports written to {self._output_dir}/")

    def _print_multiseed_verdict(self, ms: MultiSeedReport) -> None:
        labels = {
            "guards_help": "✅ Phase 0 guards HELPED (stable across seeds)",
            "guards_hurt": "⚠️ Phase 0 guards HURT (stable across seeds)",
            "guards_neutral": "➖ Guards made no significant difference (stable)",
        }
        print()
        print(f"═══ Multi-seed verdict ({ms.seeds} seeds, D-052) ═══")
        print(f"{labels[ms.verdict]}")
        print(
            f"  mean overall (median): {ms.mean_on_median:.2f} (on) vs "
            f"{ms.mean_off_median:.2f} (off) → {ms.mean_delta:+.2f}"
        )
        print(f"  improved: {ms.improved_count}, regressed: {ms.regressed_count}")
        print(f"  stable: {ms.stable_count}, noisy: {ms.noisy_count}")
        print(f"  reports written to {self._output_dir}/")


def _write_pass(report: PassReport, out_dir: Path | str) -> None:
    """Write a single PassReport to JSON/Markdown (single-pass mode)."""
    import json

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval_report.json").write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    md = [
        "# Eval Report (single pass, B-060)\n",
        f"**Pass:** {report.label}\n",
        "## Summary\n",
        f"- Scenarios: {report.total}",
        f"- Passed: {report.passed} ({report.pass_rate:.0%})",
        f"- Mean overall: {report.mean_overall:.2f}",
        f"- Mean correctness: {report.mean_correctness:.2f}\n",
        "## Per-scenario\n",
        "| Scenario | Overall | Pass |",
        "|---|---|---|",
    ]
    md.extend(
        f"| {s.scenario_id} | {s.overall_score:.2f} | {'✅' if s.passed else '❌'} |"
        for s in report.scenario_scores
    )
    (out / "eval_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
