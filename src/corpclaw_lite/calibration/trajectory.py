"""Trajectory recording for calibration and eval (B-060).

Records the sequence of tool calls and results during an AgentLoop run, so the
calibration scorer / eval judge can inspect *what the agent did*, not just its
final answer.

Since the router+executor architecture (D-028) delegates execution to
subagents, the main agent's trajectory sees only ``dispatch_subagent`` calls —
not the inner ``table_query``/``excel_workbook`` calls the subagent made. To make
the inner work visible to the eval harness, the recorder supports **nested
steps**: inner tool calls are stamped with ``subagent_id`` and merged into the
parent trajectory after the dispatch returns. ``tool_calls_sequence()`` filters
them out by default (backward-compatible with the calibration scorer); pass
``include_subagent=True`` to see the full path including delegated work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Trajectory", "TrajectoryRecorder", "TrajectoryStep"]


@dataclass
class TrajectoryStep:
    """One recorded step: a tool call, its result, or the final answer.

    ``subagent_id`` is set for steps that belong to a subagent's inner run
    (captured via :meth:`TrajectoryRecorder.record_nested`). It is ``None`` for
    steps the main agent executed directly.
    """

    step_type: str  # "tool_call" | "tool_result" | "final_answer"
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    content: str | None = None
    timestamp_ms: float = 0.0
    subagent_id: str | None = None


@dataclass
class Trajectory:
    """Immutable snapshot of a finished run's trajectory."""

    scenario_id: str
    steps: list[TrajectoryStep] = field(default_factory=lambda: list[TrajectoryStep]())
    final_answer: str = ""
    iterations: int = 0
    tools_used: list[str] = field(default_factory=lambda: list[str]())
    duration_ms: float = 0.0
    status: str = "ok"
    skills_selected: list[str] = field(default_factory=lambda: list[str]())

    def tool_calls_sequence(self, *, include_subagent: bool = False) -> list[str]:
        """Ordered list of tool names called.

        By default only the main agent's direct calls are returned (the
        calibration scorer relies on this). Pass ``include_subagent=True`` to
        also list tools called inside dispatched subagents, in execution order.
        """
        out: list[str] = []
        for step in self.steps:
            if step.step_type != "tool_call" or not step.tool_name:
                continue
            if step.subagent_id is not None and not include_subagent:
                continue
            out.append(step.tool_name)
        return out

    def dispatch_subagent_ids(self) -> list[str]:
        """The subagent ids the main agent dispatched to, in call order."""
        ids: list[str] = []
        for step in self.steps:
            if (
                step.step_type == "tool_call"
                and step.tool_name == "dispatch_subagent"
                and step.tool_args
            ):
                sid = step.tool_args.get("subagent_id")
                if isinstance(sid, str):
                    ids.append(sid)
        return ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "steps": [
                {
                    "step_type": s.step_type,
                    "tool_name": s.tool_name,
                    "tool_args": s.tool_args,
                    "tool_result": (s.tool_result or "")[:500],
                    "content": (s.content or "")[:500],
                    "timestamp_ms": round(s.timestamp_ms, 2),
                    "subagent_id": s.subagent_id,
                }
                for s in self.steps
            ],
            "final_answer": self.final_answer,
            "iterations": self.iterations,
            "tools_used": self.tools_used,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "skills_selected": self.skills_selected,
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

    def record_tool_result(self, tool_name: str, result: str) -> None:
        """Record a tool's result."""
        self._steps.append(
            TrajectoryStep(
                step_type="tool_result",
                tool_name=tool_name,
                tool_result=result,
                timestamp_ms=time.monotonic() * 1000 - self._start_ms,
            )
        )

    def record_nested(self, subagent_id: str, steps: list[TrajectoryStep]) -> None:
        """Merge a subagent's inner steps into this (parent) trajectory.

        Each step is re-stamped with ``subagent_id`` so callers can tell main
        calls from delegated ones. Called by the parent AgentLoop after a
        ``dispatch_subagent`` call returns.
        """
        now = time.monotonic() * 1000 - self._start_ms
        for step in steps:
            step.subagent_id = subagent_id
            if step.timestamp_ms == 0.0:
                step.timestamp_ms = now
            self._steps.append(step)

    def record_skills(self, skills: list[str]) -> None:
        """Record which skills were selected for this scenario."""
        self._skills_selected = skills

    def finalize(
        self,
        final_answer: str,
        *,
        iterations: int = 0,
        tools_used: list[str] | None = None,
        duration_ms: float = 0.0,
        status: str = "ok",
    ) -> Trajectory:
        """Freeze the recorded steps into an immutable Trajectory."""
        self._steps.append(
            TrajectoryStep(
                step_type="final_answer",
                content=final_answer,
                timestamp_ms=time.monotonic() * 1000 - self._start_ms,
            )
        )
        return Trajectory(
            scenario_id=self._scenario_id,
            steps=list(self._steps),
            final_answer=final_answer,
            iterations=iterations,
            tools_used=tools_used or [],
            duration_ms=duration_ms,
            status=status,
            skills_selected=list(self._skills_selected),
        )
