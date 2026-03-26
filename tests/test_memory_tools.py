"""Tests for memory_store and memory_recall tools + SQLiteMemory fact storage."""

from __future__ import annotations

import pytest

from corpclaw_lite.extensions.tools.builtin.memory import MemoryRecallTool, MemoryStoreTool
from corpclaw_lite.memory.sqlite import SQLiteMemory
from corpclaw_lite.users.models import User


@pytest.fixture
def memory(tmp_path) -> SQLiteMemory:
    return SQLiteMemory(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def user() -> User:
    return User(id=42, name="Alice", department="dev")


# ── SQLiteMemory fact methods ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_and_recall_fact(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "name", "Alice")
    await memory.store_fact("u1", "role", "Engineer")

    facts = await memory.recall_facts("u1")
    keys = {f["key"] for f in facts}
    assert "name" in keys
    assert "role" in keys
    assert any(f["value"] == "Alice" for f in facts)


@pytest.mark.asyncio
async def test_store_fact_upsert(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "city", "Moscow")
    await memory.store_fact("u1", "city", "London")

    facts = await memory.recall_facts("u1")
    city_facts = [f for f in facts if f["key"] == "city"]
    assert len(city_facts) == 1
    assert city_facts[0]["value"] == "London"


@pytest.mark.asyncio
async def test_recall_facts_with_query(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "language", "Python")
    await memory.store_fact("u1", "framework", "Django")
    await memory.store_fact("u1", "hobby", "chess")

    results = await memory.recall_facts("u1", query="Py")
    assert len(results) == 1
    assert results[0]["key"] == "language"


@pytest.mark.asyncio
async def test_recall_facts_empty(memory: SQLiteMemory) -> None:
    facts = await memory.recall_facts("nonexistent")
    assert facts == []


@pytest.mark.asyncio
async def test_clear_facts(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "a", "1")
    await memory.store_fact("u1", "b", "2")
    await memory.clear_facts("u1")
    assert await memory.recall_facts("u1") == []


# ── MemoryStoreTool ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_store_requires_user(memory: SQLiteMemory) -> None:
    tool = MemoryStoreTool(memory)
    result = await tool.execute(key="name", value="Test")
    assert "Error" in result
    assert "User context" in result


@pytest.mark.asyncio
async def test_memory_store_and_recall_roundtrip(memory: SQLiteMemory, user: User) -> None:
    store = MemoryStoreTool(memory)
    recall = MemoryRecallTool(memory)

    res = await store.execute(key="name", value="Alice", user=user)
    assert "Stored" in res

    res = await recall.execute(user=user)
    assert "name" in res
    assert "Alice" in res


@pytest.mark.asyncio
async def test_memory_recall_with_query_tool(memory: SQLiteMemory, user: User) -> None:
    store = MemoryStoreTool(memory)
    recall = MemoryRecallTool(memory)

    await store.execute(key="language", value="Python", user=user)
    await store.execute(key="hobby", value="chess", user=user)

    res = await recall.execute(query="Python", user=user)
    assert "language" in res
    assert "chess" not in res


@pytest.mark.asyncio
async def test_memory_recall_empty_tool(memory: SQLiteMemory, user: User) -> None:
    recall = MemoryRecallTool(memory)
    res = await recall.execute(user=user)
    assert "No facts stored" in res


@pytest.mark.asyncio
async def test_memory_recall_requires_user(memory: SQLiteMemory) -> None:
    tool = MemoryRecallTool(memory)
    result = await tool.execute()
    assert "Error" in result
