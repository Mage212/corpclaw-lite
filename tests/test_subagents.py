from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.agent.loop import RunStats
from corpclaw_lite.agent.subagent import SubagentDispatcher
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User


class DummyProvider:
    pass


class DummyToolA(Tool):
    name = "tool_a"
    description = ""
    params = []
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        return "A"


class DummyToolB(Tool):
    name = "tool_b"
    description = ""
    params = []
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        return "B"


@pytest.mark.asyncio
async def test_subagent_dispatcher():
    registry = ToolRegistry()
    registry.register(DummyToolA())
    registry.register(DummyToolB())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(), main_registry=registry, settings=AgentSettings()
    )  # type: ignore

    spec = SubagentSpec(
        id="test_agent", name="Test Agent", description="Testing", allowed_tools=["tool_a"]
    )
    user = User(id=1, name="User", department="dev")

    # Instead of running the real loop which requires a real provider, we patch AgentLoop.run
    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(return_value=("Subagent result", RunStats()))

        res = await dispatcher.dispatch(spec, user, "Do something")

        assert res == "Subagent result"
        call_args = MockLoop.call_args
        agent_config = call_args.args[0] if call_args.args else call_args.kwargs["config"]
        isolated_registry = agent_config.registry
        assert "tool_a" in isolated_registry._tools
        assert "tool_b" not in isolated_registry._tools
        assert agent_config.enforce_tool_permissions is False


@pytest.mark.asyncio
async def test_subagent_loop_keeps_allowed_tools_even_if_department_lacks_direct_tool_access():
    """Subagent allowed_tools are the authority inside the isolated subagent loop."""
    registry = ToolRegistry()
    registry.register(DummyToolA())
    registry.register(DummyToolB())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(), main_registry=registry, settings=AgentSettings()
    )  # type: ignore

    spec = SubagentSpec(
        id="data-agent",
        name="Data Agent",
        description="Data work",
        allowed_tools=["tool_a"],
    )
    user = User(id=1, name="User", department="marketing")

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(return_value=("Subagent result", RunStats()))

        await dispatcher.dispatch(spec, user, "Analyze data")

        call_args = MockLoop.call_args
        agent_config = call_args.args[0] if call_args.args else call_args.kwargs["config"]
        assert agent_config.enforce_tool_permissions is False
        assert set(agent_config.registry.items()) == {"tool_a"}


@pytest.mark.asyncio
async def test_subagent_prompt_loading(tmp_path: Path) -> None:
    """Subagent loads system prompt from prompt_path when the file exists."""
    prompt_file = tmp_path / "test_agent.md"
    prompt_file.write_text("# Test Agent\nYou are a test agent.", encoding="utf-8")

    registry = ToolRegistry()
    registry.register(DummyToolA())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
    )

    spec = SubagentSpec(
        id="prompt_agent",
        name="Prompt Agent",
        description="Fallback description",
        allowed_tools=["*"],
        prompt_path=str(prompt_file),
    )
    user = User(id=1, name="User", department="dev")

    captured_system: list[str] = []

    async def _capture_run(
        u: object, msg: str, system_prompt: str | None = None
    ) -> tuple[str, RunStats]:
        captured_system.append(system_prompt or "")
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something")

    # system_prompt kwarg should contain the loaded file content, not the fallback
    assert captured_system
    assert "# Test Agent" in captured_system[0]
    assert "Fallback description" not in captured_system[0]


@pytest.mark.asyncio
async def test_subagent_prompt_fallback_when_missing() -> None:
    """Subagent falls back to description when prompt_path file does not exist."""
    registry = ToolRegistry()
    registry.register(DummyToolA())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
    )

    spec = SubagentSpec(
        id="missing_prompt_agent",
        name="Missing Prompt Agent",
        description="Fallback description here",
        allowed_tools=["*"],
        prompt_path="/nonexistent/path/prompt.md",
    )
    user = User(id=1, name="User", department="dev")

    captured_system: list[str] = []

    async def _capture_run(
        u: object, msg: str, system_prompt: str | None = None
    ) -> tuple[str, RunStats]:
        captured_system.append(system_prompt or "")
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something")

    assert captured_system
    assert "Fallback description here" in captured_system[0]


@pytest.mark.asyncio
async def test_subagent_system_prompt_passed_as_kwarg() -> None:
    """system_prompt is passed as a named kwarg to loop.run(), not concatenated into msg."""
    registry = ToolRegistry()
    registry.register(DummyToolA())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
    )

    spec = SubagentSpec(
        id="kwarg_agent",
        name="Kwarg Agent",
        description="Specialized instructions",
        allowed_tools=["*"],
    )
    user = User(id=1, name="User", department="dev")

    captured_args: list[tuple[str, str | None]] = []

    async def _capture(
        u: object, msg: str, system_prompt: str | None = None
    ) -> tuple[str, RunStats]:
        captured_args.append((msg, system_prompt))
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture)

        await dispatcher.dispatch(spec, user, "Actual task text")

    assert captured_args
    task_msg, sys_prompt = captured_args[0]
    # task context goes into msg, system prompt goes into system_prompt kwarg
    assert task_msg == "Actual task text"
    assert sys_prompt is not None
    assert "Kwarg Agent" in sys_prompt or "Specialized instructions" in sys_prompt


# ── DispatchSubagentTool tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_subagent_tool_unknown_id() -> None:
    """Unknown subagent_id returns Error string listing available IDs."""
    from unittest.mock import AsyncMock, MagicMock

    from corpclaw_lite.extensions.subagents.base import SubagentSpec
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool

    registry = SubagentRegistry()
    registry.register(
        SubagentSpec(id="known", name="Known", description="desc", allowed_tools=["*"])
    )

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value="ok")

    tool = DispatchSubagentTool(dispatcher, registry)
    user = User(id=1, name="U", department="dev")

    result = await tool.execute(subagent_id="unknown_id", task="do it", user=user)

    assert "Error" in result
    assert "unknown_id" in result
    assert "known" in result


@pytest.mark.asyncio
async def test_dispatch_subagent_tool_no_user() -> None:
    """user=None returns Error string."""
    from unittest.mock import MagicMock

    from corpclaw_lite.extensions.subagents.base import SubagentSpec
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool

    registry = SubagentRegistry()
    registry.register(
        SubagentSpec(id="agent", name="Agent", description="desc", allowed_tools=["*"])
    )

    tool = DispatchSubagentTool(MagicMock(), registry)

    result = await tool.execute(subagent_id="agent", task="do it", user=None)

    assert "Error" in result
    assert "User context" in result or "user" in result.lower()


@pytest.mark.asyncio
async def test_dispatch_subagent_tool_dispatches() -> None:
    """Valid call delegates to SubagentDispatcher.dispatch() and returns its result."""
    from unittest.mock import AsyncMock, MagicMock

    from corpclaw_lite.extensions.subagents.base import SubagentSpec
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool

    spec = SubagentSpec(id="worker", name="Worker", description="desc", allowed_tools=["*"])
    registry = SubagentRegistry()
    registry.register(spec)

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value="subagent result")

    tool = DispatchSubagentTool(dispatcher, registry)
    user = User(id=1, name="U", department="dev")

    result = await tool.execute(subagent_id="worker", task="do the work", user=user)

    assert result == "subagent result"
    dispatcher.dispatch.assert_called_once_with(spec, user, "do the work")


# ── Department filtering tests ───────────────────────────────────────────────


def test_subagent_registry_department_filtering() -> None:
    """P1-6: get_allowed_subagents filters by user department."""
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry

    registry = SubagentRegistry()
    registry.register(
        SubagentSpec(
            id="marketing_agent",
            name="Marketing",
            description="Marketing tasks",
            allowed_departments=["marketing"],
        )
    )
    registry.register(
        SubagentSpec(
            id="global_agent",
            name="Global",
            description="Available to all",
            allowed_departments=["*"],
        )
    )
    registry.register(
        SubagentSpec(
            id="dev_agent",
            name="Dev",
            description="Dev only",
            allowed_departments=["dev", "engineering"],
        )
    )

    marketing_user = User(id=1, name="M", department="marketing")
    dev_user = User(id=2, name="D", department="dev")
    hr_user = User(id=3, name="H", department="hr")

    marketing_agents = registry.get_allowed_subagents(marketing_user)
    dev_agents = registry.get_allowed_subagents(dev_user)
    hr_agents = registry.get_allowed_subagents(hr_user)

    # Marketing user sees marketing_agent + global_agent
    assert {s.id for s in marketing_agents} == {"marketing_agent", "global_agent"}
    # Dev user sees dev_agent + global_agent
    assert {s.id for s in dev_agents} == {"dev_agent", "global_agent"}
    # HR user only sees global_agent (wildcard)
    assert {s.id for s in hr_agents} == {"global_agent"}


# ── Skill injection in subagents ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subagent_receives_matched_skills() -> None:
    """When skill_matcher and skill_registry are provided, matched skills are injected."""
    registry = ToolRegistry()
    registry.register(DummyToolA())

    # Create mock skill matcher that returns a skill
    from corpclaw_lite.extensions.skills.base import Skill

    matched_skill = Skill(
        id="test_skill",
        description="Test skill for subagent",
        allowed_for=["*"],
        instructions="Do the test thing carefully.",
    )

    mock_matcher = MagicMock()
    mock_matcher.match.return_value = [matched_skill]

    mock_skill_registry = MagicMock()
    mock_skill_registry.get_allowed_skills.return_value = [matched_skill]

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
        skill_matcher=mock_matcher,
        skill_registry=mock_skill_registry,
    )

    spec = SubagentSpec(
        id="skill_agent",
        name="Skill Agent",
        description="Agent with skills",
        allowed_tools=["*"],
    )
    user = User(id=1, name="User", department="dev")

    captured_system: list[str] = []

    async def _capture_run(
        u: object, msg: str, system_prompt: str | None = None
    ) -> tuple[str, RunStats]:
        captured_system.append(system_prompt or "")
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something with test skill")

    assert captured_system
    # Skill block should be present in system prompt
    assert "## Available Skills" in captured_system[0]
    assert "test_skill" in captured_system[0]
    assert "Do the test thing carefully." in captured_system[0]


@pytest.mark.asyncio
async def test_subagent_no_skills_when_matcher_none() -> None:
    """Without skill_matcher, no skills are injected into subagent prompt."""
    registry = ToolRegistry()
    registry.register(DummyToolA())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
        skill_matcher=None,
        skill_registry=None,
    )

    spec = SubagentSpec(
        id="noskill_agent",
        name="NoSkill Agent",
        description="Agent without skills",
        allowed_tools=["*"],
    )
    user = User(id=1, name="User", department="dev")

    captured_system: list[str] = []

    async def _capture_run(
        u: object, msg: str, system_prompt: str | None = None
    ) -> tuple[str, RunStats]:
        captured_system.append(system_prompt or "")
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something")

    assert captured_system
    # No skill block should be present
    assert "## Available Skills" not in captured_system[0]
