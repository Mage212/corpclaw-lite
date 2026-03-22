from unittest.mock import AsyncMock, patch
from typing import Any

import pytest

from corpclaw_lite.agent.subagent import SubagentDispatcher
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
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
