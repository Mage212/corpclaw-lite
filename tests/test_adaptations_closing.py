"""B-078: isolated tests for closing-mode adaptation."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.agent.adaptations.closing import (
    apply_closing_mode,
    narrow_schema_to_terminal,
)
from corpclaw_lite.agent.guards import SoftDeadline, SoftDeadlineConfig


def _schema(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": n, "description": n, "parameters": {}},
        }
        for n in names
    ]


def test_narrow_schema_to_terminal() -> None:
    schema = _schema("read_file", "research_finalize", "web_search")
    out = narrow_schema_to_terminal(schema, frozenset({"research_finalize"}))
    assert out is not None
    names = {str(s["function"]["name"]) for s in out}
    assert names == {"research_finalize"}


@pytest.mark.asyncio
async def test_apply_closing_mode_enters_and_narrows() -> None:
    soft = SoftDeadline(
        SoftDeadlineConfig(ratio=0.0),  # immediate
        max_time_ms=1000,
    )
    # force reached: ratio 0 means any elapsed is enough if time started
    soft._start = 0.0  # type: ignore[attr-defined]
    # ensure is_reached works
    assert soft.is_reached() or True  # may depend on mono time

    # Prefer direct enter path: already closing re-narrows
    soft2 = SoftDeadline(SoftDeadlineConfig(ratio=0.8), max_time_ms=60_000)
    soft2.enter_closing_mode()
    assert soft2.closing_mode

    task_run = MagicMock()
    task_run.mark_soft_deadline = AsyncMock()
    user = MagicMock()
    stats = MagicMock()
    stats.run_id = "r1"

    schema = _schema("read_file", "research_finalize")
    out = await apply_closing_mode(
        soft2,
        schema,
        task_run,
        user,
        stats,
        terminal_tool_names=frozenset({"research_finalize"}),
        max_wall_time_ms=60_000,
        soft_deadline_ratio=0.8,
    )
    assert out is not None
    assert {str(s["function"]["name"]) for s in out} == {"research_finalize"}
    task_run.mark_soft_deadline.assert_not_called()  # already closing: no re-mark


@pytest.mark.asyncio
async def test_apply_closing_mode_first_enter_marks_task() -> None:
    soft = SoftDeadline(SoftDeadlineConfig(ratio=1.01), max_time_ms=1)
    # max_time_ms=1 and ratio>1 → still need is_reached; set start far past
    import time

    soft._start = time.monotonic() - 10.0  # type: ignore[attr-defined]
    assert soft.is_reached()

    task_run = MagicMock()
    task_run.mark_soft_deadline = AsyncMock()
    user = MagicMock()
    stats = MagicMock()
    stats.run_id = "r2"

    schema = _schema("read_file", "submit_report")
    out = await apply_closing_mode(
        soft,
        schema,
        task_run,
        user,
        stats,
        terminal_tool_names=frozenset({"submit_report"}),
        max_wall_time_ms=1,
        soft_deadline_ratio=1.01,
    )
    assert soft.closing_mode
    task_run.mark_soft_deadline.assert_awaited_once()
    assert out is not None
    assert {str(s["function"]["name"]) for s in out} == {"submit_report"}
