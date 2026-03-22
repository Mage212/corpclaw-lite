from collections.abc import AsyncIterator
from typing import Any

import pytest

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
async def test_agent_loop_budget_exceeded_returns_string(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """BudgetExceededError is caught internally — run() returns a string, not raises."""
    settings = AgentSettings(max_steps=2)

    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="no_tool", arguments={})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="2", name="no_tool", arguments={})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="3", name="no_tool", arguments={})]),
        ]
    )
    loop = AgentLoop(provider, empty_registry, settings)

    result = await loop.run(test_user, "Loop forever")
    assert isinstance(result, str)
    assert "resource limit" in result.lower() or "budget" in result.lower()
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_history_order(test_user: User, empty_registry: ToolRegistry) -> None:
    """History messages must appear BEFORE the current user message in context."""
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from unittest.mock import MagicMock

    # Mock memory returning 2 history entries
    memory = MagicMock(spec=SQLiteMemory)
    memory.get_history.return_value = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]

    captured_messages: list[list[dict]] = []

    class CapturingProvider(MockProvider):
        async def chat(self, messages, tools=None, system=None):  # type: ignore[override]
            captured_messages.append(list(messages))
            return LLMResponse(content="done")

    provider = CapturingProvider(responses=[])
    loop = AgentLoop(provider, empty_registry, AgentSettings(), memory=memory)
    await loop.run(test_user, "new question")

    msgs = captured_messages[0]
    # Find positions
    roles = [(m.get("role"), m.get("content")) for m in msgs]
    user_contents = [c for r, c in roles if r == "user"]
    # "old question" must come before "new question"
    assert user_contents.index("old question") < user_contents.index("new question")
