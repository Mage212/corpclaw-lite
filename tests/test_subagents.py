from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

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
    async def execute(self, **kwargs: Any) -> str: return "A"

class DummyToolB(Tool):
    name = "tool_b"
    description = ""
    params = []
    risk_level = RiskLevel.LOW
    async def execute(self, **kwargs: Any) -> str: return "B"


@pytest.mark.asyncio
async def test_subagent_dispatcher():
    registry = ToolRegistry()
    registry.register(DummyToolA())
    registry.register(DummyToolB())
    
    dispatcher = SubagentDispatcher(provider=DummyProvider(), main_registry=registry, settings=AgentSettings())  # type: ignore
    
    spec = SubagentSpec(
        id="test_agent",
        name="Test Agent",
        description="Testing",
        allowed_tools=["tool_a"]
    )
    user = User(id=1, name="User", department="dev")
    
    # Instead of running the real loop which requires a real provider, we patch AgentLoop.run
    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(return_value="Subagent result")
        
        res = await dispatcher.dispatch(spec, user, "Do something")
        
        assert res == "Subagent result"
        # Verify the AgentLoop was created with an isolated registry containing ONLY "tool_a"
        call_args = MockLoop.call_args.kwargs
        isolated_registry = call_args["registry"]
        assert "tool_a" in isolated_registry._tools
        assert "tool_b" not in isolated_registry._tools


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

    captured_message: list[str] = []

    async def _capture_run(u: object, msg: str) -> str:
        captured_message.append(msg)
        return "done"

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something")

    # The full_message should start with the loaded prompt, not the fallback
    assert captured_message
    assert "# Test Agent" in captured_message[0]
    assert "Fallback description" not in captured_message[0]


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

    captured_message: list[str] = []

    async def _capture_run(u: object, msg: str) -> str:
        captured_message.append(msg)
        return "done"

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something")

    assert captured_message
    assert "Fallback description here" in captured_message[0]
