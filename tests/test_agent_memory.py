from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
from corpclaw_lite.channels.web.chat_store import WebChatStore
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import LLMResponse, Provider, ToolCall
from corpclaw_lite.memory.sqlite import SQLiteMemory
from corpclaw_lite.users.models import User


@pytest.fixture
def mock_provider():
    provider = AsyncMock(spec=Provider)
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
async def test_agent_loop_with_session_context_store(
    tmp_path: Path, mock_provider, mock_registry, test_user
) -> None:
    """B-104: history loads from ChatContextStore, not SQLiteMemory.messages."""
    db = tmp_path / "sqlite.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id=str(test_user.id), section="chat")
    await store.append_context(
        session_id=session_id,
        user_id=str(test_user.id),
        role="user",
        content="What is my name?",
    )
    await store.append_context(
        session_id=session_id,
        user_id=str(test_user.id),
        role="assistant",
        content="Your name is Test Loop User.",
    )

    settings = AgentSettings(max_steps=5, max_tool_calls=5, max_wall_time_ms=5000)
    loop = AgentLoop(
        AgentConfig(
            provider=mock_provider,
            registry=mock_registry,
            settings=settings,
            chat_context_store=store,
        )
    )

    result, _ = await loop.run(test_user, "Can you remind me again?", session_id=session_id)
    assert result == "I remember now."

    mock_provider.chat.assert_called_once()
    messages = mock_provider.chat.call_args[1]["messages"]
    user_texts = [m["content"] for m in messages if m.get("role") == "user"]
    assistant_texts = [m["content"] for m in messages if m.get("role") == "assistant"]

    assert "What is my name?" in user_texts
    assert any("Current user request:\nCan you remind me again?" in text for text in user_texts)
    assert any(isinstance(t, str) and "Your name is Test Loop User." in t for t in assistant_texts)

    # Transcript persisted only in context store (B-103).
    ctx = await store.list_context(session_id, user_id=str(test_user.id))
    roles = [m["role"] for m in ctx]
    assert roles.count("user") >= 2
    assert any(m.get("content") == "I remember now." for m in ctx if m.get("role") == "assistant")
    assert all(m.get("role") != "system" for m in ctx)


@pytest.mark.asyncio
async def test_tool_protocol_replaces_system_marker_in_context_store(tmp_path: Path) -> None:
    """Structured calls/results are durable; no authoritative system marker is stored."""

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

    db = tmp_path / "marker_test.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    user = User(id=900000042, name="Marker Test", department="engineering")
    session_id = await ws.create_session(user_id=str(user.id), section="chat")

    loop = AgentLoop(
        AgentConfig(
            provider=provider,
            registry=registry,
            settings=AgentSettings(),
            chat_context_store=store,
        )
    )
    result, stats = await loop.run(user, "Normalize my file", session_id=session_id)

    assert result == "File normalized successfully."
    assert stats.tools_used == ["normalize_excel"]

    ctx = await store.list_context(session_id, user_id=str(user.id))
    assistant_msgs = [m for m in ctx if m["role"] == "assistant"]
    assert any("File normalized successfully." in str(m.get("content", "")) for m in assistant_msgs)
    assert all(m["role"] != "system" for m in ctx)
    assert any(
        m.get("role") == "assistant"
        and m.get("tool_calls")
        and m["tool_calls"][0]["function"]["name"] == "normalize_excel"
        for m in ctx
    )


@pytest.mark.asyncio
async def test_facts_still_use_sqlite_memory(tmp_path: Path) -> None:
    """B-106: SQLiteMemory is facts-only (messages API removed)."""
    mem = SQLiteMemory(str(tmp_path / "facts.db"))
    await mem.store_fact("u1", "role", "engineer")
    facts = await mem.recall_facts("u1", limit=5)
    assert any(f["key"] == "role" and f["value"] == "engineer" for f in facts)
    assert not hasattr(mem, "add_message")
    assert not hasattr(mem, "get_history")


@pytest.mark.asyncio
async def test_base_system_prompt_reads_bootstrap_live(tmp_path: Path) -> None:
    """B-111 audit: SOUL base comes from bootstrap each call (mtime/overlay), not
    a factory-frozen default_system_prompt snapshot."""
    from unittest.mock import AsyncMock

    from corpclaw_lite.config.bootstrap import BootstrapLoader
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.llm.base import LLMResponse, Provider

    bootstrap_dir = tmp_path / "bootstrap"
    bootstrap_dir.mkdir()
    soul = bootstrap_dir / "SOUL.md"
    soul.write_text("SOUL v1", encoding="utf-8")
    bootstrap = BootstrapLoader(bootstrap_dir)

    provider = AsyncMock(spec=Provider)
    provider.chat.return_value = LLMResponse(content="ok", tool_calls=[])
    loop = AgentLoop(
        AgentConfig(
            provider=provider,
            registry=ToolRegistry(),
            settings=AgentSettings(max_steps=2, max_tool_calls=2, max_wall_time_ms=5000),
            default_system_prompt="STALE SNAPSHOT",
            bootstrap=bootstrap,
        )
    )

    user = User(id=1, name="U", department="engineering")
    # Live text, not the frozen snapshot.
    assert loop._base_system_prompt_text() == "SOUL v1"
    preview = await loop.assemble_system_prompt(user)
    assert preview is not None
    assert "SOUL v1" in preview
    assert "STALE SNAPSHOT" not in preview

    soul.write_text("SOUL v2", encoding="utf-8")
    assert loop._base_system_prompt_text() == "SOUL v2"
