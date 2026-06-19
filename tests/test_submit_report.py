"""Tests for B-057: SubmitReportTool — explicit terminator for subagent loops."""

from __future__ import annotations

import pytest

from corpclaw_lite.extensions.tools.base import TOOL_ERROR_PREFIX
from corpclaw_lite.extensions.tools.builtin.submit_report import SubmitReportTool


@pytest.mark.asyncio
async def test_execute_returns_result_text() -> None:
    tool = SubmitReportTool()
    result = await tool.execute(result_text="Report content here")
    assert result == "Report content here"


@pytest.mark.asyncio
async def test_execute_strips_nothing() -> None:
    """Whitespace-only text is treated as empty (rejected), but legitimate
    surrounding whitespace is preserved in accepted results."""
    tool = SubmitReportTool()
    result = await tool.execute(result_text="  padded answer  ")
    assert result == "  padded answer  "


@pytest.mark.asyncio
async def test_execute_error_on_empty_string() -> None:
    """An empty submission returns an Error-prefixed result, which keeps the
    loop alive (terminal-termination in loop.py guards on non-Error results)."""
    tool = SubmitReportTool()
    result = await tool.execute(result_text="")
    assert result.startswith(TOOL_ERROR_PREFIX)
    assert "non-empty" in result


@pytest.mark.asyncio
async def test_execute_error_on_whitespace_only() -> None:
    tool = SubmitReportTool()
    result = await tool.execute(result_text="   \n\t  ")
    assert result.startswith(TOOL_ERROR_PREFIX)


@pytest.mark.asyncio
async def test_execute_coerces_non_string_result() -> None:
    """LLM tool-call args may arrive as non-strings (numbers, objects); coerce
    rather than crash."""
    tool = SubmitReportTool()
    result = await tool.execute(result_text=42)  # type: ignore[arg-type]
    assert result == "42"


def test_attributes_terminal_and_not_parallel_safe() -> None:
    """terminal=True enables loop termination; parallel_safe=False keeps it in
    the sequential branch where terminal-termination actually fires."""
    tool = SubmitReportTool()
    assert tool.terminal is True
    assert tool.parallel_safe is False
    assert tool.risk_level.name == "LOW"


def test_should_return_direct_true_by_default() -> None:
    """Default should_return_direct returns self.terminal — must be True so
    loop.py returns the result without an extra LLM re-paraphrase."""
    tool = SubmitReportTool()
    assert tool.should_return_direct({}, "some result") is True


def test_name_and_params() -> None:
    tool = SubmitReportTool()
    assert tool.name == "submit_report"
    param_names = [p.name for p in tool.params]
    assert param_names == ["result_text"]
    assert tool.params[0].required is True
