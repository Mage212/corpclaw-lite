"""Group A — Basic single-turn debug integration tests.

Tests in this file verify that the agent pipeline is alive and responding
before moving on to tool-heavy or multi-step scenarios.

Run:
    uv run pytest tests/debug/test_A_single_turn.py -v -s
"""

from __future__ import annotations

import pytest

from corpclaw_lite.agent.loop import AgentLoop, RunStats
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User

from .helpers import DebugAssertions, summarise_run

# All tests in this module require a real LLM
pytestmark = [pytest.mark.integration, pytest.mark.llm_required]


# ---------------------------------------------------------------------------
# A1 — Agent responds to a greeting (no tools)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A1_simple_greeting(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """Agent answers a simple greeting without using any tools."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(test_user, "Привет! Как дела?")

    assert isinstance(reply, str) and len(reply) > 0, "Reply must not be empty"
    DebugAssertions.assert_status_ok(stats)
    DebugAssertions.assert_no_tool_error(reply)
    print(f"\n[A1] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# A2 — Agent solves trivial math (no tools)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A2_simple_math(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """Agent answers 2+2=4 without using any tools, in at most 2 iterations."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(test_user, "Сколько будет 2 + 2? Просто число, без объяснений.")

    DebugAssertions.assert_status_ok(stats)
    DebugAssertions.assert_reply_contains(reply, "4")
    assert stats.iterations <= 2, (
        f"Simple math should require ≤ 2 iterations, got {stats.iterations}"
    )
    print(f"\n[A2] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# A3 — Agent replies in the language of the request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A3_russian_response(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """Agent responds to a Russian query with a non-empty reply."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(
        test_user,
        "Напиши одно предложение о том, что такое Python.",
    )

    DebugAssertions.assert_status_ok(stats)
    assert len(reply) > 10, "Reply is suspiciously short"
    # Verify there is at least some Cyrillic content (agent should respond in Russian)
    has_cyrillic = any("\u0400" <= ch <= "\u04ff" for ch in reply)
    assert has_cyrillic, (
        f"Expected Cyrillic characters in reply (agent should use Russian).\n"
        f"Reply: {reply[:300]}"
    )
    print(f"\n[A3] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# A4 — Reply is always a string, status is always set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A4_reply_is_string(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """AgentLoop.run() always returns (str, RunStats) regardless of content."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(test_user, "Назови одну столицу Европы.")

    assert isinstance(reply, str), f"reply must be str, got {type(reply)}"
    assert isinstance(stats, RunStats), f"stats must be RunStats, got {type(stats)}"
    assert stats.status in {"ok", "budget", "loop", "timeout", "error"}
    assert stats.duration_ms > 0
    print(f"\n[A4] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# A5 — No tools are used for a pure knowledge question
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A5_no_tools_for_knowledge(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """Agent answers a factual question without invoking any tools."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(
        test_user,
        "Кто написал роман «Война и мир»? Одним словом.",
    )

    DebugAssertions.assert_status_ok(stats)
    # For a pure knowledge question the agent SHOULD NOT use tools
    # (no file to read, no script to run)
    assert stats.tools_used == [], (
        f"Expected no tool calls for a knowledge question, "
        f"got tools_used={stats.tools_used}"
    )
    DebugAssertions.assert_reply_contains(reply, "Толстой")
    print(f"\n[A5] {summarise_run(reply, stats)}")
