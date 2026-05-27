from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
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

    # Add fake history (methods are now async)
    await mem.add_message(str(test_user.id), "user", "What is my name?")
    await mem.add_message(str(test_user.id), "assistant", "Your name is Test Loop User.")

    settings = AgentSettings(max_steps=5, max_tool_calls=5, max_wall_time_ms=5000)

    loop = AgentLoop(
        AgentConfig(provider=mock_provider, registry=mock_registry, settings=settings, memory=mem)
    )

    result, _ = await loop.run(test_user, "Can you remind me again?")

    assert result == "I remember now."

    # Check that it extracted the messages from provider's chat mock and sent the right context
    # provider.chat was called. We inspect its kwargs.
    mock_provider.chat.assert_called_once()

    call_args = mock_provider.chat.call_args[1]
    messages = call_args["messages"]

    # We expect: Context Msg (User) -> Context Msg (Assistant) -> Current User Msg
    # System prompt is now passed via system= kwarg, not in messages
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
    hist = await mem.get_history(str(test_user.id))
    # 2 history + user msg + assistant msg + system execution record = 5
    assert len(hist) == 5

    # Last two: assistant response + system execution record
    assert hist[-2]["role"] == "assistant"
    assert "I remember now." in hist[-2]["content"]
    assert hist[-1]["role"] == "system"
    assert "Tools called in this turn: none" in hist[-1]["content"]
    assert hist[-3]["role"] == "user"
    assert hist[-3]["content"] == "Can you remind me again?"


@pytest.mark.asyncio
async def test_tool_marker_saved_in_memory(tmp_path):
    """When tools are used, the saved assistant message must include a tool-usage marker.

    This prevents hallucination: the model can see proof of real tool calls
    in its conversation history for subsequent requests.
    """
    from corpclaw_lite.config.settings import AgentSettings
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.llm.base import LLMResponse, ToolCall

    class FakeTool:
        name = "normalize_excel"
        description = "Normalize Excel"
        params = []
        terminal = False

        async def execute(self, **kwargs):
            return "Normalized 100 rows."

    registry = ToolRegistry()
    registry._tools["normalize_excel"] = FakeTool()  # type: ignore

    provider = AsyncMock(spec=Provider)
    provider.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="tc1", name="normalize_excel", arguments={"path": "test.xlsx"})
            ],
        ),
        LLMResponse(content="File normalized successfully."),
    ]

    mem = SQLiteMemory(str(tmp_path / "marker_test.db"))
    user = User(id=900000042, name="Marker Test", department="engineering")

    loop = AgentLoop(
        AgentConfig(
            provider=provider,
            registry=registry,
            settings=AgentSettings(),
            memory=mem,
        )
    )
    result, stats = await loop.run(user, "Normalize my file")

    assert result == "File normalized successfully."
    assert stats.tools_used == ["normalize_excel"]

    # Check that the saved assistant message has clean content (marker in reasoning)
    hist = await mem.get_history(str(user.id))
    assistant_msg = [m for m in hist if m["role"] == "assistant"][-1]
    assert "File normalized successfully." in assistant_msg["content"]

    # Execution record is saved as system message
    system_msgs = [m for m in hist if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert "normalize_excel" in system_msgs[0]["content"]
