"""Tests for TaskRun checkpoint journal (B-036)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpclaw_lite.agent.task_run import PHASE_PARTIAL, PHASE_STARTED, TaskRun
from corpclaw_lite.users.models import User


def _user() -> User:
    return User(id=9, telegram_id=9, name="T", department="engineering")


@pytest.mark.asyncio
async def test_initialize_creates_state(tmp_path: Path) -> None:
    user = _user()
    tr = TaskRun(tmp_path)
    run_dir = await tr.initialize(
        user, "run-1", subagent_id="research-agent", parent_run_id="parent-1"
    )
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == PHASE_STARTED
    assert state["status"] == "running"
    assert state["subagent_id"] == "research-agent"
    assert state["parent_run_id"] == "parent-1"
    assert state["soft_deadline_reached"] is False


@pytest.mark.asyncio
async def test_record_tool_call_appends_journal(tmp_path: Path) -> None:
    user = _user()
    tr = TaskRun(tmp_path)
    await tr.initialize(user, "run-2")
    await tr.record_tool_call(
        user, "run-2", name="research_search", args_hash="abc", status="ok", duration_ms=12.3
    )
    await tr.record_tool_call(
        user,
        "run-2",
        name="research_fetch_source",
        args_hash="def",
        status="error",
        duration_ms=5.0,
        error="boom",
    )
    journal = (tr.run_dir(user, "run-2") / "journal.jsonl").read_text(encoding="utf-8")
    entries = [json.loads(line) for line in journal.splitlines() if line.strip()]
    assert len(entries) == 2
    assert entries[0]["tool"] == "research_search"
    assert entries[0]["status"] == "ok"
    assert entries[1]["status"] == "error"
    assert entries[1]["error"] == "boom"
    # args_hash recorded, not full args (secrecy)
    assert entries[0]["args_hash"] == "abc"


@pytest.mark.asyncio
async def test_generate_handoff_writes_markdown_and_phase(tmp_path: Path) -> None:
    user = _user()
    tr = TaskRun(tmp_path)
    await tr.initialize(user, "run-3")
    await tr.record_tool_call(
        user, "run-3", name="research_search", args_hash="h", status="ok", duration_ms=10.0
    )
    handoff = await tr.generate_handoff(
        user, "run-3", partial_result="## Partial\nfindings", reason="timeout"
    )
    assert "## Partial" in handoff
    assert "research_search" in handoff  # journal summary included
    handoff_file = tr.run_dir(user, "run-3") / "handoff.md"
    assert handoff_file.exists()
    state = json.loads((tr.run_dir(user, "run-3") / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == PHASE_PARTIAL


@pytest.mark.asyncio
async def test_mark_soft_deadline_sets_flag(tmp_path: Path) -> None:
    user = _user()
    tr = TaskRun(tmp_path)
    await tr.initialize(user, "run-4")
    await tr.mark_soft_deadline(user, "run-4")
    state = json.loads((tr.run_dir(user, "run-4") / "state.json").read_text(encoding="utf-8"))
    assert state["soft_deadline_reached"] is True


@pytest.mark.asyncio
async def test_set_phase_updates_state(tmp_path: Path) -> None:
    user = _user()
    tr = TaskRun(tmp_path)
    await tr.initialize(user, "run-5")
    await tr.set_phase(user, "run-5", "tool_executed")
    state = json.loads((tr.run_dir(user, "run-5") / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "tool_executed"
