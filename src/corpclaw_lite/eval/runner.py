"""Eval runner — execute scenarios through a real AgentLoop and score them (B-060).

Fork of :class:`corpclaw_lite.calibration.runner.CalibrationRunner`, adapted for
the eval harness:

- Multi-turn scenarios: each turn is a separate ``AgentLoop.run()`` call sharing
  memory (conversation continuity). Memory is cleared only between scenarios,
  not between turns.
- GAIA-style scoring: the deterministic scorer runs first; the LLM judge is
  invoked only when correctness cannot be settled deterministically. The judge
  is optional (closed-circuit deployments without a cloud provider fall back to
  deterministic-only scoring).
- Binary fixtures via ``copy_from_corpus`` (xlsx/csv copied from a corpus dir).
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from corpclaw_lite.calibration.trajectory import Trajectory, TrajectoryRecorder
from corpclaw_lite.eval.scenarios import EvalScenario, ScenarioTurn
from corpclaw_lite.eval.scorer import DeterministicScorer
from corpclaw_lite.eval.scores import (
    ScenarioScore,
    TurnScore,
    aggregate_scenario,
)

__all__ = [
    "EvalRunner",
    "ScenarioRunResult",
    "TurnRunResult",
]

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from corpclaw_lite.agent.loop import AgentLoop
    from corpclaw_lite.eval.judge import LLMJudge
    from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)


# All-zero dimension scores for crashed/failed turns.
_DIMENSIONS = (
    "correctness",
    "tool_selection",
    "context_retention",
    "completeness",
    "efficiency",
    "personality",
    "error_recovery",
)
_ZERO_SCORES: dict[str, float] = dict.fromkeys(_DIMENSIONS, 0.0)


class TurnRunResult:
    """Captured run output for one turn (before scoring)."""

    __slots__ = ("turn", "final_answer", "tools_called", "trajectory", "status", "error")

    def __init__(
        self,
        turn: ScenarioTurn,
        final_answer: str,
        tools_called: list[str],
        trajectory: Trajectory,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        self.turn = turn
        self.final_answer = final_answer
        self.tools_called = tools_called
        self.trajectory = trajectory
        self.status = status
        self.error = error


class ScenarioRunResult:
    """Captured run output for one scenario (all turns, before scoring)."""

    __slots__ = ("scenario", "turns")

    def __init__(self, scenario: EvalScenario, turns: list[TurnRunResult]) -> None:
        self.scenario = scenario
        self.turns = turns


class EvalRunner:
    """Run eval scenarios through a real AgentLoop, then score them.

    Each scenario is isolated: workspace files are materialised before and
    removed after; memory is cleared between scenarios. Within a multi-turn
    scenario, memory persists across turns so the agent retains conversation
    context (this is what context_retention measures).
    """

    def __init__(
        self,
        agent_loop: AgentLoop,
        user: User,
        system_prompt: str | None,
        workspace_dir: Path,
        corpus_dir: Path | None = None,
        few_shots: list[dict[str, Any]] | None = None,
        deterministic_scorer: DeterministicScorer | None = None,
        judge: LLMJudge | None = None,
    ) -> None:
        self._agent_loop = agent_loop
        self._user = user
        self._system_prompt = system_prompt
        self._workspace_dir = workspace_dir
        self._corpus_dir = corpus_dir
        self._few_shots = few_shots
        self._scorer = deterministic_scorer or DeterministicScorer()
        self._judge = judge

    async def run_all(
        self,
        scenarios: list[EvalScenario],
        on_progress: Callable[[str, bool, int, int], None] | None = None,
    ) -> list[ScenarioScore]:
        """Run all scenarios and return scored results.

        The process working directory is changed to ``workspace_dir`` for the
        duration of the run so that the agent's file tools (list_files,
        read_file, table_query) resolve paths against the materialised setup
        files rather than the project root. The original cwd is restored in a
        finally block.
        """
        results: list[ScenarioScore] = []
        original_cwd = Path.cwd()
        os.chdir(self._workspace_dir)
        try:
            for idx, scenario in enumerate(scenarios):
                logger.info(
                    "[eval] Running scenario %d/%d: %s (%d turn(s))",
                    idx + 1,
                    len(scenarios),
                    scenario.id,
                    len(scenario.turns),
                )
                self._setup_workspace(scenario)
                try:
                    run_result = await self._run_scenario(scenario)
                    score = await self._score_scenario_async(run_result)
                    results.append(score)
                    log_fn = logger.info if score.passed else logger.warning
                    log_fn(
                        "[eval] %s %s: overall=%.2f",
                        "✅" if score.passed else "❌",
                        scenario.id,
                        score.overall_score,
                    )
                except Exception as e:  # noqa: BLE001 — crash must not abort the run
                    logger.exception("[eval] Scenario %s crashed", scenario.id)
                    results.append(self._crash_score(scenario, e))
                finally:
                    self._cleanup_workspace(scenario)
                    if self._agent_loop.memory:
                        await self._agent_loop.memory.clear(str(self._user.id))
                        # B-060: also clear stored memory_facts so a scenario's
                        # memory_store calls do not leak into the next scenario
                        # (memory.clear() only wipes the conversation, not facts).
                        clear_facts = getattr(self._agent_loop.memory, "clear_facts", None)
                        if callable(clear_facts):
                            await cast("Callable[[str], Awaitable[None]]", clear_facts)(
                                str(self._user.id)
                            )

                if on_progress is not None:
                    on_progress(scenario.id, results[-1].passed, idx + 1, len(scenarios))
        finally:
            os.chdir(original_cwd)
        return results

    # ──────────────────────────── execution ──────────────────────────────

    async def _run_scenario(self, scenario: EvalScenario) -> ScenarioRunResult:
        """Execute every turn of the scenario, sharing memory across turns."""
        turn_results: list[TurnRunResult] = []
        for turn_idx, turn in enumerate(scenario.turns):
            recorder = TrajectoryRecorder(f"{scenario.id}#turn{turn_idx}")
            try:
                answer, stats = await self._agent_loop.run(
                    user=self._user,
                    message=turn.user_message,
                    system_prompt=self._system_prompt,
                    trajectory_recorder=recorder,
                    few_shots=self._few_shots,
                )
                trajectory = recorder.finalize(
                    final_answer=answer,
                    iterations=stats.iterations,
                    tools_used=stats.tools_used,
                    duration_ms=stats.duration_ms,
                    status=stats.status,
                )
                turn_results.append(
                    TurnRunResult(
                        turn=turn,
                        final_answer=answer,
                        tools_called=list(stats.tools_used),
                        trajectory=trajectory,
                        status=stats.status,
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("[eval] Turn %d of %s crashed", turn_idx + 1, scenario.id)
                crash_traj = recorder.finalize(final_answer=f"CRASH: {e}", status="error")
                turn_results.append(
                    TurnRunResult(
                        turn=turn,
                        final_answer=f"CRASH: {e}",
                        tools_called=[],
                        trajectory=crash_traj,
                        status="error",
                        error=str(e),
                    )
                )
                # A crashed turn ends the scenario: later turns have no answer
                # to build on.
                break
        return ScenarioRunResult(scenario=scenario, turns=turn_results)

    # ───────────────────────────── scoring ───────────────────────────────

    async def _score_scenario_async(self, run_result: ScenarioRunResult) -> ScenarioScore:
        turn_scores: list[TurnScore] = []
        for idx, tr in enumerate(run_result.turns):
            turn_scores.append(await self._score_turn(tr, idx))
        return aggregate_scenario(run_result.scenario.id, turn_scores)

    async def _score_turn(self, tr: TurnRunResult, turn_index: int) -> TurnScore:
        """Score one turn, then decorate with trajectory observability."""
        from corpclaw_lite.eval.judge import render_transcript

        score = await self._score_turn_inner(tr, turn_index)
        # B-060: persist what the agent actually did for debugging.
        score.final_answer = tr.final_answer
        score.tools_called = list(tr.tools_called)
        score.transcript = render_transcript(tr.trajectory)
        return score

    async def _score_turn_inner(self, tr: TurnRunResult, turn_index: int) -> TurnScore:
        if tr.status == "error":
            return self._zero_turn(f"Turn crashed: {tr.error}")
        det = self._scorer.score_turn(tr.turn, tr.final_answer, tr.trajectory)
        if not det.judge_needed:
            return det.score
        if self._judge is None:
            return self._no_judge_fallback(tr)
        try:
            return await self._judge.judge_turn(tr.turn, tr.final_answer, tr.trajectory, turn_index)
        except Exception as e:  # noqa: BLE001
            logger.warning("[eval] Judge failed on turn %d: %s; falling back", turn_index, e)
            return self._no_judge_fallback(tr)

    def _zero_turn(self, reasoning: str) -> TurnScore:
        return TurnScore(
            scores=dict(_ZERO_SCORES),
            overall_score=0.0,
            passed=False,
            failure_category="run_error",
            reasoning=reasoning,
        )

    def _no_judge_fallback(self, tr: TurnRunResult) -> TurnScore:
        from corpclaw_lite.eval.scores import decide_pass, recompute_overall

        scores: dict[str, float] = {
            "correctness": 5.0,
            "tool_selection": 6.0,
            "context_retention": 6.0,
            "completeness": 5.0,
            "efficiency": 6.0,
            "personality": 6.0,
            "error_recovery": 6.0,
        }
        overall = recompute_overall(scores)
        return TurnScore(
            scores=scores,
            overall_score=overall,
            passed=decide_pass(scores, overall),
            reasoning="No judge available; conservative fallback scores applied.",
        )

    def _crash_score(self, scenario: EvalScenario, error: Exception) -> ScenarioScore:
        zero = TurnScore(
            scores=dict(_ZERO_SCORES),
            overall_score=0.0,
            passed=False,
            failure_category="run_error",
            reasoning=f"Scenario crashed: {error}",
        )
        return ScenarioScore(scenario_id=scenario.id, turns=[zero], overall_score=0.0, passed=False)

    # ────────────────────────── workspace mgmt ───────────────────────────

    def _setup_workspace(self, scenario: EvalScenario) -> None:
        if scenario.setup is None:
            return
        for rel_path, content in scenario.setup.files:
            full = self._workspace_dir / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
        for dest, src in scenario.setup.copy_from_corpus:
            if self._corpus_dir is None:
                logger.warning(
                    "[eval] Scenario %s wants %s from corpus but no corpus_dir set",
                    scenario.id,
                    src,
                )
                continue
            src_path = self._corpus_dir / src
            if not src_path.exists():
                logger.warning("[eval] Corpus fixture missing: %s", src_path)
                continue
            dest_path = self._workspace_dir / dest
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_path, dest_path)
        # Deterministic PNG fixtures for vision scenarios.
        for dest, generator_id in scenario.setup.generated_images:
            from corpclaw_lite.eval.vision_fixtures import generate_image

            dest_path = self._workspace_dir / dest
            try:
                generate_image(generator_id, dest_path)
            except ValueError as e:
                logger.warning("[eval] %s: %s", scenario.id, e)

    def _cleanup_workspace(self, scenario: EvalScenario) -> None:
        if scenario.setup is None:
            return
        all_paths = [p for p, _ in scenario.setup.files]
        all_paths += [d for d, _ in scenario.setup.copy_from_corpus]
        all_paths += [d for d, _ in scenario.setup.generated_images]
        for rel_path in all_paths:
            full = self._workspace_dir / rel_path
            if full.exists():
                full.unlink()
        for rel_path in all_paths:
            parent = (self._workspace_dir / rel_path).parent
            while parent != self._workspace_dir and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
