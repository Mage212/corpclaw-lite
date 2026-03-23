from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import LLMResponse, Provider
from corpclaw_lite.memory.sqlite import SQLiteMemory
from corpclaw_lite.users.models import User


@pytest.fixture
def mock_provider():
    provider = AsyncMock(spec=Provider)
    # Return a basic text completion
    provider.chat.return_value = LLMResponse(content="I remember now.", tool_calls=[])
    return provider


@pytest.fixture
def mock_registry():
    registry = AsyncMock(spec=ToolRegistry)
    registry.to_schemas.return_value = []
    return registry


@pytest.fixture
def test_user():
    return User(id=777, name="Test Loop User", department="HR")


@pytest.mark.asyncio
async def test_agent_loop_with_memory(tmp_path, mock_provider, mock_registry, test_user):
    # Setup real SQLiteMemory but point it to temp file
    db_file = tmp_path / "sqlite.db"
    mem = SQLiteMemory(str(db_file))

    # Add fake history
    mem.add_message(str(test_user.id), "user", "What is my name?")
    mem.add_message(str(test_user.id), "assistant", "Your name is Test Loop User.")

    settings = AgentSettings(max_steps=5, max_tool_calls=5, max_wall_time_ms=5000)

    loop = AgentLoop(provider=mock_provider, registry=mock_registry, settings=settings, memory=mem)

    result = await loop.run(test_user, "Can you remind me again?")

    assert result == "I remember now."

    # Check that it extracted the messages from provider's chat mock and sent the right context
    # provider.chat was called. We inspect its kwargs.
    mock_provider.chat.assert_called_once()

    call_args = mock_provider.chat.call_args[1]
    messages = call_args["messages"]

    # We expect: System -> Context Msg (User) -> Context Msg (Assistant) -> Current User Msg
    assert len(messages) >= 3

    # In CorpClaw Lite, messages are raw dictionaries
    user_texts = [m["content"] for m in messages if m.get("role") == "user"]
    assistant_texts = [m["content"] for m in messages if m.get("role") == "assistant"]

    assert "What is my name?" in user_texts
    assert "Can you remind me again?" in user_texts

    found_assistant = False
    for t in assistant_texts:
        if isinstance(t, str) and "Your name is Test Loop User." in t:
            found_assistant = True
    assert found_assistant, "Should have loaded assistant message from memory"

    # Check that new messages were appended to memory
    hist = mem.get_history(str(test_user.id))
    assert len(hist) == 4

    # newest message is last in SQLite get_history order
    assert hist[-1]["role"] == "assistant"
    assert hist[-1]["content"] == "I remember now."
    assert hist[-2]["role"] == "user"
    assert hist[-2]["content"] == "Can you remind me again?"
