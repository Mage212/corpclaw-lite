"""Structured trajectory recorder for agent execution.

Records every step of an AgentLoop run (LLM calls, tool calls, tool results,
final answer) in a machine-readable format suitable for post-mortem analysis,
calibration scoring, and audit logging.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Trajectory",
    "TrajectoryRecorder",
    "TrajectoryStep",
]


@dataclass
class TrajectoryStep:
    """Single step in agent execution."""

    step_type: str  # "tool_call" | "tool_result" | "final_answer"
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    content: str | None = None
    timestamp_ms: float = 0.0


@dataclass
class Trajectory:
    """Full execution trace for one scenario run."""

    scenario_id: str
    steps: list[TrajectoryStep] = field(default_factory=lambda: list[TrajectoryStep]())
    final_answer: str = ""
    iterations: int = 0
    tools_used: list[str] = field(default_factory=lambda: list[str]())
    duration_ms: float = 0.0
    status: str = "ok"
    skills_selected: list[str] = field(default_factory=lambda: list[str]())

    def tool_calls_sequence(self) -> list[str]:
        """Return ordered list of tool names called."""
        return [
            s.tool_name
            for s in self.steps
            if s.step_type == "tool_call" and s.tool_name is not None
        ]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "scenario_id": self.scenario_id,
            "final_answer": self.final_answer,
            "iterations": self.iterations,
            "tools_used": self.tools_used,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "skills_selected": self.skills_selected,
            "steps": [
                {
                    "step_type": s.step_type,
                    "tool_name": s.tool_name,
                    "tool_args": s.tool_args,
                    "tool_result": (
                        s.tool_result[:500]
                        if s.tool_result and len(s.tool_result) > 500
                        else s.tool_result
                    ),
                    "content": (
                        s.content[:500] if s.content and len(s.content) > 500 else s.content
                    ),
                    "timestamp_ms": s.timestamp_ms,
                }
                for s in self.steps
            ],
        }


class TrajectoryRecorder:
    """Records execution steps during an AgentLoop run.

    Usage::

        recorder = TrajectoryRecorder("scenario_id")
        # ... pass to AgentLoop.run() ...
        trajectory = recorder.finalize("final answer", run_stats)
    """

    def __init__(self, scenario_id: str) -> None:
        self._scenario_id = scenario_id
        self._steps: list[TrajectoryStep] = []
        self._start_ms = time.monotonic() * 1000
        self._skills_selected: list[str] = []

    def record_tool_call(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        """Record a tool invocation."""
        self._steps.append(
            TrajectoryStep(
                step_type="tool_call",
                tool_name=tool_name,
                tool_args=tool_args,
                timestamp_ms=time.monotonic() * 1000 - self._start_ms,
            )
        )

    def record_skills(self, skills: list[str]) -> None:
        """Record which skills were selected for this scenario."""
        self._skills_selected = skills

    def record_tool_result(self, tool_name: str, result: str) -> None:
        """Record a tool execution result."""
        self._steps.append(
            TrajectoryStep(
                step_type="tool_result",
                tool_name=tool_name,
                tool_result=result,
                timestamp_ms=time.monotonic() * 1000 - self._start_ms,
            )
        )

    def finalize(
        self,
        final_answer: str,
        iterations: int = 0,
        tools_used: list[str] | None = None,
        duration_ms: float = 0.0,
        status: str = "ok",
    ) -> Trajectory:
        """Build the final Trajectory from recorded steps."""
        self._steps.append(
            TrajectoryStep(
                step_type="final_answer",
                content=final_answer,
                timestamp_ms=time.monotonic() * 1000 - self._start_ms,
            )
        )
        return Trajectory(
            scenario_id=self._scenario_id,
            steps=self._steps,
            final_answer=final_answer,
            iterations=iterations,
            tools_used=tools_used or [],
            duration_ms=duration_ms,
            status=status,
            skills_selected=self._skills_selected,
        )
