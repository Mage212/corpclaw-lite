import asyncio
from typing import Any, AsyncIterator

import pytest

from corpclaw_lite.agent.guards import BudgetExceededError
from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import LLMResponse, Provider, StreamChunk, ToolCall
from corpclaw_lite.users.models import User


class MockProvider(Provider):
    def __init__(self, responses: list[LLMResponse]):
        self.responses = responses
        self.call_count = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        raise NotImplementedError()


@pytest.fixture
def test_user() -> User:
    return User(id=1, name="Test", department="QA")


@pytest.fixture
def empty_registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.mark.asyncio
async def test_agent_loop_basic(test_user: User, empty_registry: ToolRegistry) -> None:
    provider = MockProvider(
        responses=[
            LLMResponse(content="Hello! How can I help?"),
        ]
    )
    loop = AgentLoop(provider, empty_registry, AgentSettings())
    res = await loop.run(test_user, "Hi")
    assert res == "Hello! How can I help?"
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_agent_loop_tool_call(test_user: User, empty_registry: ToolRegistry) -> None:
    # A fake tool that just returns "tool output"
    class FakeTool:
        name = "test_tool"
        description = "A fake tool"
        params = []
        async def execute(self, **kwargs: Any) -> str:
            return "tool output"

    empty_registry._tools["test_tool"] = FakeTool()  # type: ignore

    provider = MockProvider(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="test_tool", arguments={})]
            ),
            LLMResponse(content="I ran the tool and it worked."),
        ]
    )
    loop = AgentLoop(provider, empty_registry, AgentSettings())
    res = await loop.run(test_user, "Run test tool")
    assert res == "I ran the tool and it worked."
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_agent_loop_budget_guard(test_user: User, empty_registry: ToolRegistry) -> None:
    # Set max steps to 2
    settings = AgentSettings(max_steps=2)

    # Provider always returns tool calls -> infinite loop
    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="no_tool", arguments={})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="2", name="no_tool", arguments={})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="3", name="no_tool", arguments={})]),
        ]
    )
    loop = AgentLoop(provider, empty_registry, settings)

    with pytest.raises(BudgetExceededError):
        await loop.run(test_user, "Loop")
    
    assert provider.call_count == 2
