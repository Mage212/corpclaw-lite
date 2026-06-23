"""LLM judge — 7-dimension rubric scoring via a cloud provider (B-060, step 4).

Invoked only when the deterministic scorer cannot settle correctness (no
zero-rule fired and no exact match). The judge receives the scenario, the agent
transcript (tool calls + final answer), and the judge_turn.md rubric, and
returns per-dimension scores. The harness recomputes the weighted overall and
the pass/fail decision deterministically — LLM arithmetic is never trusted
(same approach as the GAIA reference recompute_turn_score).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, cast

from corpclaw_lite.calibration.trajectory import Trajectory
from corpclaw_lite.eval.scenarios import ScenarioTurn
from corpclaw_lite.eval.scores import (
    TurnScore,
    decide_pass,
    recompute_overall,
)
from corpclaw_lite.llm.base import Provider
from corpclaw_lite.paths import PROJECT_ROOT

__all__ = ["JudgeError", "LLMJudge"]

logger = logging.getLogger(__name__)

_DEFAULT_RUBRIC_PATH = PROJECT_ROOT / "config" / "eval" / "judge_turn.md"

_JUDGE_SYSTEM = (
    "You are a strict but fair evaluator of an office AI agent. You judge the "
    "agent's response to a scenario against a rubric. Respond ONLY with the "
    "JSON object specified in the rubric — no markdown fences, no prose."
)


class JudgeError(Exception):
    """Raised when the judge model returns an unparseable or malformed verdict."""


def render_transcript(trajectory: Trajectory) -> str:
    """Render a trajectory as a readable transcript (tool calls + results).

    Nested subagent tool calls are prefixed with ``[<subagent_id>]`` so the full
    execution path — including delegated work — is visible. Used both by the
    LLM judge (in its prompt) and by the eval report (for observability).
    """
    if not trajectory.steps:
        return "(no tool calls recorded)"
    lines: list[str] = []
    for step in trajectory.steps:
        prefix = ""
        if step.subagent_id:
            prefix = f"[{step.subagent_id}] "
        if step.step_type == "tool_call" and step.tool_name:
            args = json.dumps(step.tool_args or {}, ensure_ascii=False)
            lines.append(f"- {prefix}TOOL CALL: {step.tool_name}({args})")
        elif step.step_type == "tool_result" and step.tool_name:
            result = (step.tool_result or "").strip()
            if len(result) > 300:
                result = result[:300] + "…"
            lines.append(f"  {prefix}→ RESULT: {result}")
    if not lines:
        return "(no tool calls recorded; agent answered directly)"
    return "\n".join(lines)


class LLMJudge:
    """Score a turn via a cloud model using the 7-dimension rubric.

    When ``ensemble > 1``, the judge queries the provider N times per turn and
    aggregates the verdicts by per-dimension median. This reduces judge
    variance (the cloud judge itself is not perfectly stable across calls) —
    orthogonal to multi-seed, which reduces *agent* sampling noise. A single
    failed ensemble member is tolerated (median of the survivors).
    """

    _DIMENSIONS = (
        "correctness",
        "tool_selection",
        "context_retention",
        "completeness",
        "efficiency",
        "personality",
        "error_recovery",
    )

    def __init__(
        self,
        provider: Provider,
        rubric_path: Path | str | None = None,
        agent_tools: list[str] | None = None,
        ensemble: int = 1,
    ) -> None:
        self._provider = provider
        path = Path(rubric_path) if rubric_path else _DEFAULT_RUBRIC_PATH
        if not path.exists():
            raise FileNotFoundError(f"Judge rubric not found: {path}")
        self._rubric = path.read_text(encoding="utf-8")
        self._agent_tools = agent_tools
        self._ensemble = max(1, ensemble)

    async def judge_turn(
        self,
        turn: ScenarioTurn,
        final_answer: str,
        trajectory: Trajectory,
        turn_index: int = 0,
    ) -> TurnScore:
        """Score one turn via the cloud judge.

        The transcript is rendered from the trajectory (tool calls + results +
        the final answer). Pre-check and zero-rules have already run in the
        deterministic layer and did not fire — the judge scores the remaining
        ambiguity.
        """
        prompt = self._build_prompt(turn, final_answer, trajectory, turn_index)

        if self._ensemble <= 1:
            return await self._judge_once(prompt)

        # Ensemble: query the provider N times, take per-dimension median.
        verdicts: list[dict[str, Any]] = []
        for _ in range(self._ensemble):
            try:
                verdicts.append(await self._query_and_parse(prompt))
            except (JudgeError, Exception):  # noqa: BLE001 — tolerate one bad member
                logger.debug("[eval] Ensemble member failed, continuing")
        if not verdicts:
            raise JudgeError("All ensemble judge calls failed")
        return self._build_ensemble_score(verdicts)

    async def _judge_once(self, prompt: str) -> TurnScore:
        """Single judge call → TurnScore (no ensemble)."""
        verdict = await self._query_and_parse(prompt)
        return self._build_score(verdict)

    async def _query_and_parse(self, prompt: str) -> dict[str, Any]:
        """Call the provider once and parse the JSON verdict."""
        response = await self._provider.chat(
            messages=[{"role": "user", "content": prompt}],
            system=_JUDGE_SYSTEM,
        )
        content = (response.content or "").strip()
        return self._parse_verdict(content)

    # ─────────────────────────── prompt assembly ─────────────────────────

    def _build_prompt(
        self,
        turn: ScenarioTurn,
        final_answer: str,
        trajectory: Trajectory,
        turn_index: int,
    ) -> str:
        transcript = self._render_transcript(trajectory)
        expected_line = (
            "null (the answer does NOT exist in the source; the agent should say it doesn't know)"
            if turn.expected_answer is None
            else f'"{turn.expected_answer}"'
        )
        behavior_line = ", ".join(turn.expected_tools) if turn.expected_tools else "(not specified)"
        criteria_line = turn.success_criteria or "(none)"
        tool_surface = self._render_tool_surface()
        return f"""# Scenario Turn Evaluation

## Rubric
{self._rubric}
{tool_surface}
## Scenario Turn (turn #{turn_index + 1} of {turn_index + 1})

- **User question**: {turn.user_message}
- **expected_answer**: {expected_line}
- **expected_tools** (behavioural expectation): {behavior_line}
- **success_criteria**: {criteria_line}

## Agent Transcript

{transcript}

## Agent's Final Answer

{final_answer or "(empty)"}

## Your Task

Apply the rubric STEP 1 (pre-check), STEP 2 (zero-rules), STEP 3 (score each
dimension 0-10) and STEP 4 (pass/fail). Return ONLY the JSON object specified in
the rubric's OUTPUT FORMAT section."""

    def _render_tool_surface(self) -> str:
        """Render the agent's actual tool surface as a judge-facing note.

        The main agent follows a router+executor pattern (D-028): it has only
        inspection + routing tools. Execution tools (table_query, write_file,
        convert_format, ...) are only available inside dispatched subagents.
        Without this note the judge penalises the agent for using
        ``dispatch_subagent`` instead of an execution tool it does not have.
        """
        if not self._agent_tools:
            return ""
        tools_list = ", ".join(self._agent_tools)
        return f"""
## IMPORTANT — Tools Available to the Agent
The agent under test has ONLY these tools: {tools_list}.

Office execution tools (table_query, write_file, convert_format,
normalize_excel, excel_workbook, chart_generate, edit_file, exec_script,
pdf_reader, diff_text) are NOT directly available to the agent. The agent
MUST delegate via ``dispatch_subagent`` to a subagent that has them. Delegation
is the CORRECT behaviour, not a tool-selection error. Do NOT penalise
tool_selection merely because the agent used ``dispatch_subagent`` instead of
an execution tool it lacks — instead judge whether the delegation was to the
right subagent and whether the task was completed. Nested subagent tool calls
appear in the transcript prefixed with the subagent id (e.g. ``[data-agent]``).
"""

    def _render_transcript(self, trajectory: Trajectory) -> str:
        """Render the trajectory as a readable transcript for the judge.

        Nested subagent tool calls are prefixed with ``[<subagent_id>]`` so the
        judge can see the full execution path, including delegated work.
        """
        return render_transcript(trajectory)

    # ────────────────────────────── parsing ──────────────────────────────

    def _parse_verdict(self, content: str) -> dict[str, Any]:
        """Parse the judge's JSON verdict, tolerating markdown fences."""
        text = content.strip()
        # Strip markdown code fences if present.
        if text.startswith("```"):
            text = _strip_code_fence(text)
        # Try direct parse first.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Fallback: extract the first {...} block.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.error("[eval] Judge returned unparseable verdict: %s", content[:500])
        raise JudgeError(f"Judge returned unparseable JSON: {content[:200]}")

    def _build_score(self, verdict: dict[str, Any]) -> TurnScore:
        raw_scores_obj: Any = verdict.get("scores", {})
        if not isinstance(raw_scores_obj, dict):
            raise JudgeError(f"Judge 'scores' is not an object: {type(raw_scores_obj)}")
        raw_scores = cast(dict[str, Any], raw_scores_obj)
        scores: dict[str, float] = {}
        for dim in self._DIMENSIONS:
            value: Any = raw_scores.get(dim)
            if value is None:
                raise JudgeError(f"Judge missing dimension '{dim}'")
            try:
                scores[dim] = float(value)
            except (TypeError, ValueError) as e:
                raise JudgeError(f"Judge dimension '{dim}' not numeric: {value}") from e
        overall = recompute_overall(scores)
        passed = decide_pass(scores, overall)
        failure = verdict.get("failure_category")
        if not isinstance(failure, str | type(None)):
            failure = None
        reasoning = str(verdict.get("reasoning", ""))
        return TurnScore(
            scores=scores,
            overall_score=overall,
            passed=passed,
            failure_category=failure,
            reasoning=reasoning,
            judge_used=True,
        )

    def _build_ensemble_score(self, verdicts: list[dict[str, Any]]) -> TurnScore:
        """Aggregate N judge verdicts by per-dimension median.

        Numeric dimensions are median'd independently, then ``overall`` and
        ``passed`` are recomputed deterministically. ``failure_category`` and
        ``reasoning`` are taken from the verdict whose overall score is closest
        to the median overall (a deterministic tie-break that avoids arbitrary
        text averaging).
        """
        import statistics

        # Extract per-dimension scores from each verdict (tolerate partial).
        per_dim: dict[str, list[float]] = {dim: [] for dim in self._DIMENSIONS}
        parsed_overalls: list[float] = []
        for v in verdicts:
            raw = cast(dict[str, Any], v.get("scores", {}))
            dim_scores: dict[str, float] = {}
            for dim in self._DIMENSIONS:
                val: Any = raw.get(dim)
                if val is not None:
                    try:
                        dim_scores[dim] = float(val)
                        per_dim[dim].append(float(val))
                    except (TypeError, ValueError):
                        pass
            if dim_scores:
                parsed_overalls.append(recompute_overall(dim_scores))

        # Median per dimension (0.0 when no samples for that dim).
        median_scores: dict[str, float] = {
            dim: (statistics.median(vals) if vals else 0.0) for dim, vals in per_dim.items()
        }
        overall = recompute_overall(median_scores)
        passed = decide_pass(median_scores, overall)

        # Pick the verdict closest to the median overall for text fields.
        best_idx = 0
        best_dist = float("inf")
        for i, ov in enumerate(parsed_overalls):
            dist = abs(ov - overall)
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        # Map parsed_overalls index back to a verdict that had scores.
        scored_verdicts = [v for v in verdicts if isinstance(v.get("scores"), dict)]
        representative = scored_verdicts[best_idx] if scored_verdicts else verdicts[0]
        failure = representative.get("failure_category")
        if not isinstance(failure, str | type(None)):
            failure = None
        reasoning = str(representative.get("reasoning", ""))
        return TurnScore(
            scores=median_scores,
            overall_score=overall,
            passed=passed,
            failure_category=failure,
            reasoning=f"[ensemble of {len(verdicts)}] {reasoning}",
            judge_used=True,
        )


def _strip_code_fence(text: str) -> str:
    """Remove a leading ```lang and trailing ``` fence, if present."""
    lines = text.split("\n")
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)
