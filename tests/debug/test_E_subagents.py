"""Group E — Subagent delegation integration tests.

The main agent decides autonomously to call dispatch_subagent.
If it does NOT call it, the test fails — this is intentional.

Design rationale:
  LLM is given a very explicit prompt mentioning the subagent ID, but
  the final decision is LLM's. This validates real delegation behaviour.
  Tests E1–E4 go through the full agent loop.
  Tests E5–E6 directly exercise SubagentDispatcher to verify tool isolation
  and registry wiring without depending on an LLM routing decision.

Run:
    uv run pytest tests/debug/test_E_subagents.py -v -s
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User

from .helpers import DebugAssertions, summarise_run

pytestmark = [pytest.mark.integration, pytest.mark.llm_required]


# ---------------------------------------------------------------------------
# E1 — Main agent delegates filesystem task to filesystem-agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_E1_dispatch_to_filesystem_agent(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Main agent uses dispatch_subagent to delegate a filesystem listing task.

    The prompt explicitly names the subagent ID so the LLM has no ambiguity.
    If dispatch_subagent is not called, this test FAILS — that is the intent.
    """
    loop, _ = agent_stack_no_container

    # Pre-create some files for the subagent to find
    for name in ("module_a.py", "module_b.py", "config.yaml"):
        (tmp_workspace / name).write_text(f"# {name}", encoding="utf-8")

    reply, stats = await loop.run(
        test_user,
        "Используй инструмент dispatch_subagent с subagent_id='filesystem-agent' "
        "и задачей: найди все .py файлы в текущей директории и перечисли их имена.",
    )

    DebugAssertions.assert_tool_used(stats, "dispatch_subagent")
    DebugAssertions.assert_status_ok(stats)
    # The subagent's result should mention at least one .py file
    assert "module_a.py" in reply or "module_b.py" in reply or ".py" in reply, (
        f"Expected .py file names in reply from filesystem-agent.\n"
        f"{summarise_run(reply, stats)}"
    )
    print(f"\n[E1] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# E2 — Main agent delegates execution to execution-agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_E2_dispatch_to_execution_agent(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Main agent delegates a script execution task to execution-agent."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(
        test_user,
        "Используй dispatch_subagent с subagent_id='execution-agent' и задачей: "
        "выполни Python код: print('HELLO_FROM_SUBAGENT_E02') и верни вывод.",
    )

    DebugAssertions.assert_tool_used(stats, "dispatch_subagent")
    DebugAssertions.assert_status_ok(stats)
    # Use case-insensitive check: model may normalise 'HELLO' → 'Hello' in output
    assert "HELLO_FROM_SUBAGENT_E02".lower() in reply.lower(), (
        f"Expected reply to contain 'HELLO_FROM_SUBAGENT_E02' (case-insensitive).\n"
        f"Reply (first 600 chars):\n{reply[:600]}"
    )
    print(f"\n[E2] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# E3 — Main agent delegates document creation to document-agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_E3_dispatch_to_document_agent(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Main agent delegates file creation to document-agent."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(
        test_user,
        "Используй dispatch_subagent с subagent_id='document-agent' и задачей: "
        "создай файл summary_e03.md с заголовком '# Summary E03' и двумя пунктами.",
    )

    DebugAssertions.assert_tool_used(stats, "dispatch_subagent")
    DebugAssertions.assert_status_ok(stats)

    summary_file = tmp_workspace / "summary_e03.md"
    assert summary_file.exists(), (
        f"document-agent should have created summary_e03.md.\n"
        f"{summarise_run(reply, stats)}"
    )
    print(f"\n[E3] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# E4 — Main agent delegates research to research-agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_E4_dispatch_to_research_agent(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """Main agent delegates a web-fetch task to research-agent."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(
        test_user,
        "Используй dispatch_subagent с subagent_id='research-agent' и задачей: "
        "загрузи страницу https://httpbin.org/get и скажи какой HTTP статус вернул сервер.",
    )

    DebugAssertions.assert_tool_used(stats, "dispatch_subagent")
    DebugAssertions.assert_status_ok(stats)
    DebugAssertions.assert_no_tool_error(reply)
    print(f"\n[E4] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# E5 — All 4 built-in subagents are registered (direct registry check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_E5_all_subagents_registered(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """dispatch_subagent tool must hold a registry with all 4 built-in subagents."""
    _, registry = agent_stack_no_container

    dispatch_tool = registry.get("dispatch_subagent")
    assert dispatch_tool is not None, "dispatch_subagent tool is not registered"

    subagent_registry = dispatch_tool._subagent_registry  # type: ignore[attr-defined]
    all_specs = subagent_registry.list_all()
    ids = {s.id for s in all_specs}

    expected_ids = {"filesystem-agent", "execution-agent", "document-agent", "research-agent"}
    missing = expected_ids - ids
    assert not missing, (
        f"Missing subagent IDs: {missing}. Found: {ids}"
    )
    print(f"\n[E5] Registered subagents: {ids}")


# ---------------------------------------------------------------------------
# E6 — execution-agent does NOT have access to web_fetch (tool isolation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_E6_execution_agent_tool_isolation(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """execution-agent's isolated registry must NOT contain web_fetch.

    This test is direct (no LLM routing decision) to make it deterministic.
    We simulate what SubagentDispatcher.dispatch() does internally.
    """
    _, registry = agent_stack_no_container

    dispatch_tool = registry.get("dispatch_subagent")
    assert dispatch_tool is not None
    subagent_registry = dispatch_tool._subagent_registry  # type: ignore[attr-defined]

    spec = subagent_registry.get_spec("execution-agent")
    assert spec is not None, "execution-agent spec not found"

    # Simulate the isolated registry built in SubagentDispatcher.dispatch()
    from corpclaw_lite.extensions.tools.registry import ToolRegistry as TR

    isolated = TR()
    for tool_name, tool in registry.items().items():
        if "*" in spec.allowed_tools or tool_name in spec.allowed_tools:
            isolated.register(tool)

    # web_fetch must NOT be accessible to the execution agent
    assert "web_fetch" not in isolated._tools, (  # type: ignore[attr-defined]
        "execution-agent should not have access to web_fetch"
    )
    # exec_script MUST be accessible
    assert "exec_script" in isolated._tools, (  # type: ignore[attr-defined]
        "execution-agent must have exec_script in its isolated registry"
    )
    print(f"\n[E6] execution-agent tools: {list(isolated._tools.keys())}")  # type: ignore[attr-defined]
