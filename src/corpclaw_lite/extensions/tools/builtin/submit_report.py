"""B-057: SubmitReportTool — explicit terminator for subagent inner loops.

Without an explicit completion signal, local LLMs often loop or fall silent
after finishing their work in a subagent's inner AgentLoop. Calling this tool
terminates the subagent run and returns the result to the parent agent.

The termination itself relies on the existing terminal-tool machinery in
``AgentLoop.run`` (``tool.terminal`` + ``should_return_direct`` + single-call
batch): the loop returns the tool result directly without an extra LLM
re-paraphrase. No changes to loop.py are required.

Complementary to B-047 (TerminalToolMandate): B-047 is a deterministic
closing-mode guard that pushes research-agent toward ``research_finalize`` as
the wall-clock budget runs out. ``submit_report`` is a universal explicit
terminator available to *every* subagent regardless of spec, used at the
model's own discretion when its work is complete.
"""

from __future__ import annotations

from typing import Any

from corpclaw_lite.extensions.tools.base import TOOL_ERROR_PREFIX, RiskLevel, Tool, ToolParam

__all__ = [
    "SubmitReportTool",
]


class SubmitReportTool(Tool):
    """Submit a completed work result and terminate the subagent run.

    Call this when your task is done: the result is returned to the parent
    agent and your run terminates. Do not call it before you have actually
    completed the work.
    """

    name = "submit_report"
    description = (
        "Submit your completed work result. Call this when your task is done: "
        "the result is returned to the parent agent and your run terminates. "
        "Do not call it before you have actually completed the work."
    )
    params = [
        ToolParam(
            name="result_text",
            type="string",
            description="The complete result of your work, ready to return to the parent agent.",
            required=True,
        ),
    ]
    risk_level = RiskLevel.LOW
    # Must NOT be parallel-safe: terminal-termination in loop.py only fires in
    # the sequential branch (a single tool call in the batch). If this ran in
    # the parallel branch, termination would silently not trigger.
    parallel_safe = False
    # terminal=True enables AgentLoop to return this tool's result directly to
    # the user/parent without an extra LLM re-paraphrase step.
    terminal = True

    async def execute(self, *, result_text: Any = "", **kwargs: Any) -> str:
        _ = kwargs
        text = result_text if isinstance(result_text, str) else str(result_text)
        if not text.strip():
            # Error-prefixed results do NOT trigger terminal-termination
            # (loop.py guards on `not result.startswith(TOOL_ERROR_PREFIX)`),
            # so an empty submission keeps the loop alive rather than returning
            # an empty result to the parent.
            return f"{TOOL_ERROR_PREFIX} submit_report requires non-empty result_text."
        return text
