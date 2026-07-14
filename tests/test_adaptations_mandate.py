"""B-078: isolated tests for workflow-mandate adaptation."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from corpclaw_lite.agent.adaptations.mandate import (
    WORKFLOW_NUDGE_INSTRUCTION,
    apply_workflow_mandate,
)
from corpclaw_lite.agent.guards import TerminalToolMandate, TerminalToolMandateConfig


def _schema(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": n, "description": n, "parameters": {}},
        }
        for n in names
    ]


def test_mandate_disabled_noop() -> None:
    mandate = TerminalToolMandate(
        TerminalToolMandateConfig(terminal_tool="", required_before=()),
        max_time_ms=60_000,
        max_iterations=10,
    )
    schema = _schema("a", "b")
    ctx = MagicMock()
    ctx.system_prompt = "base"
    stats = MagicMock()
    stats.iterations = 1
    stats.tools_used = []
    out = apply_workflow_mandate(mandate, schema, ctx, stats)
    assert out is schema


def test_mandate_nudge_and_restrict() -> None:
    mandate = TerminalToolMandate(
        TerminalToolMandateConfig(
            terminal_tool="research_finalize",
            required_before=("research_list_facts",),
            nudge_ratio=0.0,
            restrict_ratio=0.0,
        ),
        max_time_ms=60_000,
        max_iterations=10,
    )
    # Force wall-clock elapsed high
    import time

    mandate._start = time.monotonic() - 1000.0  # type: ignore[attr-defined]

    schema = _schema("web_search", "research_list_facts", "research_finalize")
    ctx = MagicMock()
    ctx.system_prompt = "sys"
    stats = MagicMock()
    stats.iterations = 9
    stats.tools_used = ["web_search"]
    stats.run_id = "run-m"

    out = apply_workflow_mandate(mandate, schema, ctx, stats)
    assert out is not None
    names = {str(s["function"]["name"]) for s in out}
    # restrict to required_before + terminal
    assert "web_search" not in names
    assert "research_list_facts" in names
    assert "research_finalize" in names
    # nudge injected into system prompt
    assert "research_finalize" in (ctx.system_prompt or "")
    assert WORKFLOW_NUDGE_INSTRUCTION.split("{")[0][:20] in (ctx.system_prompt or "") or (
        "time budget" in (ctx.system_prompt or "")
    )
