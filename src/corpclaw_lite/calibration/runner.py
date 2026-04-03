"""Calibration runner — execute scenarios through a real AgentLoop."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from corpclaw_lite.calibration.scenarios import CalibrationScenario
from corpclaw_lite.calibration.scorer import CalibrationScorer, ScenarioResult
from corpclaw_lite.calibration.trajectory import TrajectoryRecorder

__all__ = [
    "CalibrationRunner",
]

if TYPE_CHECKING:
    from corpclaw_lite.agent.loop import AgentLoop
    from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)


class CalibrationRunner:
    """Run calibration scenarios through a real AgentLoop and score results.

    Each scenario is isolated:
    - A temporary workspace is created with setup files
    - Memory is cleared between scenarios
    - The workspace is cleaned up after each run
    """

    def __init__(
        self,
        agent_loop: AgentLoop,
        user: User,
        system_prompt: str | None,
        workspace_dir: Path,
    ) -> None:
        self._agent_loop = agent_loop
        self._user = user
        self._system_prompt = system_prompt
        self._workspace_dir = workspace_dir
        self._scorer = CalibrationScorer()

    async def run_all(
        self,
        scenarios: list[CalibrationScenario],
        on_progress: Callable[[str, bool, int, int], None] | None = None,
    ) -> list[ScenarioResult]:
        """Run all scenarios and return scored results.

        Args:
            scenarios: list of scenarios to run.
            on_progress: optional callback(scenario_id, passed, index, total).

        Returns:
            List of ScenarioResult for each scenario.
        """
        results: list[ScenarioResult] = []

        for idx, scenario in enumerate(scenarios):
            logger.info(
                "[calibration] Running scenario %d/%d: %s",
                idx + 1,
                len(scenarios),
                scenario.id,
            )

            # Setup workspace
            self._setup_workspace(scenario)

            try:
                # Run through AgentLoop with TrajectoryRecorder
                recorder = TrajectoryRecorder(scenario.id)
                answer, stats = await self._agent_loop.run(
                    user=self._user,
                    message=scenario.user_message,
                    system_prompt=self._system_prompt,
                    trajectory_recorder=recorder,
                )

                # Build trajectory from recorder
                trajectory = recorder.finalize(
                    final_answer=answer,
                    iterations=stats.iterations,
                    tools_used=stats.tools_used,
                    duration_ms=stats.duration_ms,
                    status=stats.status,
                )

                # Score
                result = self._scorer.score(scenario, trajectory)
                results.append(result)

                log_fn = logger.info if result.passed else logger.warning
                log_fn(
                    "[calibration] %s %s: %s",
                    "✅" if result.passed else "❌",
                    scenario.id,
                    result.failure_reason or "passed",
                )

            except Exception as e:
                logger.error("[calibration] Scenario %s crashed: %s", scenario.id, e)
                # Create a minimal trajectory for the crash
                crash_recorder = TrajectoryRecorder(scenario.id)
                trajectory = crash_recorder.finalize(
                    final_answer=f"CRASH: {e}",
                    status="error",
                )
                results.append(
                    ScenarioResult(
                        scenario=scenario,
                        trajectory=trajectory,
                        passed=False,
                        failure_reason=f"Scenario crashed: {e}",
                    )
                )

            finally:
                # Cleanup workspace
                self._cleanup_workspace(scenario)

                # Clear memory between scenarios for isolation
                if self._agent_loop.memory:
                    await self._agent_loop.memory.clear(str(self._user.id))

            if on_progress is not None:
                result = results[-1]
                on_progress(scenario.id, result.passed, idx + 1, len(scenarios))

        return results

    def _setup_workspace(self, scenario: CalibrationScenario) -> None:
        """Create test files from scenario setup."""
        if scenario.setup is None:
            return

        for rel_path, content in scenario.setup.files:
            full_path = self._workspace_dir / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            logger.debug("[calibration] Created setup file: %s", full_path)

    def _cleanup_workspace(self, scenario: CalibrationScenario) -> None:
        """Remove test files created during setup."""
        if scenario.setup is None:
            return

        for rel_path, _ in scenario.setup.files:
            full_path = self._workspace_dir / rel_path
            if full_path.exists():
                full_path.unlink()
                logger.debug("[calibration] Cleaned up: %s", full_path)

        # Remove empty parent dirs
        for rel_path, _ in scenario.setup.files:
            parent = (self._workspace_dir / rel_path).parent
            if parent != self._workspace_dir and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
