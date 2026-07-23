"""Tests for memory_store/recall tools + SQLiteMemory entries (B-108)."""

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
    return User(id=900000042, name="Alice", department="dev")


# ── SQLiteMemory entry methods ───────────────────────────────────────────────


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
async def test_store_entry_and_recall_entries(memory: SQLiteMemory) -> None:
    await memory.store_entry(
        "u1",
        primary_abstraction="Client INN for Acme",
        memory_value="7707083893",
        cues=["Acme", "7707083893"],
    )
    hits = await memory.recall_entries("u1", query="Acme")
    assert len(hits) >= 1
    assert hits[0]["abstraction"] == "Client INN for Acme"
    assert "7707083893" in hits[0]["cues"]


@pytest.mark.asyncio
async def test_store_fact_upsert(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "city", "Moscow")
    await memory.store_fact("u1", "city", "London")

    facts = await memory.recall_facts("u1")
    city_facts = [f for f in facts if f["key"] == "city"]
    assert len(city_facts) == 1
    assert city_facts[0]["value"] == "London"


@pytest.mark.asyncio
async def test_store_entry_merges_cues_on_upsert(memory: SQLiteMemory) -> None:
    await memory.store_entry(
        "u1",
        primary_abstraction="Prefers brief style",
        memory_value="short answers",
        cues=["brief"],
    )
    await memory.store_entry(
        "u1",
        primary_abstraction="Prefers brief style",
        memory_value="even shorter",
        cues=["concise"],
    )
    hits = await memory.recall_entries("u1")
    assert len(hits) == 1
    assert hits[0]["value"] == "even shorter"
    assert set(hits[0]["cues"]) == {"brief", "concise"}


@pytest.mark.asyncio
async def test_recall_facts_with_query(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "language", "Python")
    await memory.store_fact("u1", "framework", "Django")
    await memory.store_fact("u1", "hobby", "chess")

    results = await memory.recall_facts("u1", query="Py")
    assert len(results) >= 1
    assert any(r["key"] == "language" for r in results)


@pytest.mark.asyncio
async def test_recall_exact_cue_boost(memory: SQLiteMemory) -> None:
    await memory.store_entry(
        "u1",
        primary_abstraction="Tax id Acme LLC",
        memory_value="details about acme tax",
        cues=["7707083893", "Acme"],
    )
    await memory.store_entry(
        "u1",
        primary_abstraction="Random note",
        memory_value="something else entirely",
        cues=[],
    )
    hits = await memory.recall_entries("u1", query="7707083893", limit=5)
    assert hits
    assert hits[0]["abstraction"] == "Tax id Acme LLC"


@pytest.mark.asyncio
async def test_user_isolation(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "secret", "a")
    await memory.store_fact("u2", "secret", "b")
    f1 = await memory.recall_facts("u1")
    f2 = await memory.recall_facts("u2")
    assert f1[0]["value"] == "a"
    assert f2[0]["value"] == "b"


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


@pytest.mark.asyncio
async def test_no_legacy_memory_facts_table(memory: SQLiteMemory) -> None:
    import sqlite3

    with sqlite3.connect(memory.db_path) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "memory_entries" in tables
    assert "memory_facts" not in tables


# ── MemoryStoreTool ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_store_requires_user(memory: SQLiteMemory) -> None:
    tool = MemoryStoreTool(memory)
    result = await tool.execute(key="name", value="Test")
    assert "Error" in result


@pytest.mark.asyncio
async def test_memory_store_legacy_key_value(memory: SQLiteMemory, user: User) -> None:
    tool = MemoryStoreTool(memory)
    result = await tool.execute(user=user, key="name", value="Alice")
    assert "Stored" in result
    facts = await memory.recall_facts(str(user.id))
    assert any(f["key"] == "name" and f["value"] == "Alice" for f in facts)


@pytest.mark.asyncio
async def test_memory_store_abstraction_value_cues(memory: SQLiteMemory, user: User) -> None:
    tool = MemoryStoreTool(memory)
    result = await tool.execute(
        user=user,
        abstraction="Client Acme tax id",
        value="7707083893",
        cues='["Acme","7707083893"]',
    )
    assert "Stored" in result
    hits = await memory.recall_entries(str(user.id), query="Acme")
    assert hits and hits[0]["value"] == "7707083893"


@pytest.mark.asyncio
async def test_memory_recall_tool(memory: SQLiteMemory, user: User) -> None:
    await memory.store_entry(
        str(user.id),
        primary_abstraction="Language preference",
        memory_value="Russian",
        cues=["lang"],
    )
    tool = MemoryRecallTool(memory)
    out = await tool.execute(user=user, query="lang")
    assert "Language preference" in out
    assert "Russian" in out
