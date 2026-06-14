"""Tests for B-036: research-agent partial-handoff on subagent timeout."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from corpclaw_lite.agent.loop import RunStats
from corpclaw_lite.agent.subagent import SubagentDispatcher
from corpclaw_lite.config.settings import AgentSettings, ResearchSettings
from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.builtin.research import ResearchRuntime
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User


class DummyProvider:
    pass


def _user() -> User:
    return User(id=11, telegram_id=11, name="R", department="engineering")


def _research_dispatcher(tmp_path: Path) -> tuple[SubagentDispatcher, ResearchRuntime]:
    research_runtime = ResearchRuntime(
        settings=ResearchSettings(finalize_strict=False),
        workspace_base=tmp_path,
    )
    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore[arg-type]
        main_registry=ToolRegistry(),
        settings=AgentSettings(max_wall_time_ms=50),  # 50ms -> fast timeout
        research_runtime=research_runtime,
        workspace_base=tmp_path,
    )
    return dispatcher, research_runtime


@pytest.mark.asyncio()
async def test_research_timeout_returns_partial_report_not_bare_error(tmp_path: Path) -> None:
    user = _user()
    dispatcher, research_runtime = _research_dispatcher(tmp_path)
    spec = SubagentSpec(
        id="research-agent",
        name="Research Agent",
        description="Research",
        allowed_tools=["*"],
        direct_response=True,
    )

    fixed_run_id = "fixedsubagentrunid000"

    # Pre-store a source for the fixed run_id so the partial skeleton has content.
    research_runtime.initialize_run_mode(user, fixed_run_id, "research", language="ru")
    research_runtime.store_source(
        user,
        fixed_run_id,
        "https://example.com/a",
        "url: https://example.com/a\nstatus: 200\nsize: 10\n---\nTitle A\nEvidence A.",
    )

    async def _slow_run(*a: Any, **kw: Any) -> tuple[str, RunStats]:
        await asyncio.sleep(1.0)
        return "should not reach", RunStats()

    with (
        patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop,
        patch("corpclaw_lite.agent.subagent.uuid") as mock_uuid,
    ):
        mock_uuid.uuid4.return_value.hex = fixed_run_id
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_slow_run)

        result = await dispatcher.dispatch(spec, user, "Исследуй квантование")

    # Partial report returned, NOT bare "Subagent error: ..."
    assert not result.startswith("Subagent error:")
    assert not result.startswith("Error")
    # Language-aware skeleton built from stored source
    assert "## Краткий вывод" in result  # ru skeleton heading
    assert "https://example.com/a" in result  # stored source cited

    # handoff.md written to .task_runs/
    handoff_files = list((tmp_path / f"user_{user.workspace_key()}").rglob("handoff.md"))
    assert handoff_files, "handoff.md should be generated on partial-handoff"


@pytest.mark.asyncio()
async def test_non_research_timeout_still_returns_bare_error(tmp_path: Path) -> None:
    # MVP scope is research-only: non-research subagents still return the bare
    # timeout error (no partial-handoff infrastructure for them yet).
    user = _user()
    dispatcher, _ = _research_dispatcher(tmp_path)
    spec = SubagentSpec(
        id="filesystem-agent",
        name="FS Agent",
        description="Filesystem",
        allowed_tools=["*"],
    )

    async def _slow_run(*a: Any, **kw: Any) -> tuple[str, RunStats]:
        await asyncio.sleep(1.0)
        return "x", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_slow_run)

        result = await dispatcher.dispatch(spec, user, "List files")

    assert result.startswith("Subagent error: execution timed out")


@pytest.mark.asyncio()
async def test_research_timeout_partial_report_emits_trace(tmp_path: Path) -> None:
    from corpclaw_lite.logging import trace as trace_mod
    from corpclaw_lite.logging.trace import setup_trace_logging

    setup_trace_logging(tmp_path, enabled=True)
    try:
        user = _user()
        dispatcher, research_runtime = _research_dispatcher(tmp_path)
        spec = SubagentSpec(
            id="research-agent",
            name="Research Agent",
            description="Research",
            allowed_tools=["*"],
            direct_response=True,
        )
        research_runtime.initialize_run_mode(user, "r-trace", "research", language="en")

        async def _slow_run(*a: Any, **kw: Any) -> tuple[str, RunStats]:
            await asyncio.sleep(1.0)
            return "x", RunStats()

        with (
            patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop,
            patch("corpclaw_lite.agent.subagent.uuid") as mock_uuid,
        ):
            mock_uuid.uuid4.return_value.hex = "r-trace"
            mock_loop_instance = MockLoop.return_value
            mock_loop_instance.run = AsyncMock(side_effect=_slow_run)

            await dispatcher.dispatch(spec, user, "Conduct research on quantization")

        import json

        trace = tmp_path / "agent_trace.jsonl"
        events = [
            json.loads(line)
            for line in trace.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        names = [str(e.get("event")) for e in events]
        assert "subagent_partial_handoff" in names
    finally:
        trace_mod._trace_logger = None
