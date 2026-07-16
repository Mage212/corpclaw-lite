"""Tests for SQLiteMemory entries API (B-106 facts slim-down + B-108 Memora)."""

from __future__ import annotations

import sqlite3

import pytest

from corpclaw_lite.memory.sqlite import SQLiteMemory


@pytest.fixture
def memory(tmp_path):
    db_file = tmp_path / "test_memory.db"
    return SQLiteMemory(str(db_file))


@pytest.mark.asyncio
async def test_store_and_recall_fact(memory: SQLiteMemory) -> None:
    await memory.store_fact("user123", "role", "engineer")
    facts = await memory.recall_facts("user123")
    assert len(facts) == 1
    assert facts[0]["key"] == "role"
    assert facts[0]["value"] == "engineer"


@pytest.mark.asyncio
async def test_store_fact_upsert(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "lang", "ru")
    await memory.store_fact("u1", "lang", "en")
    facts = await memory.recall_facts("u1")
    assert len(facts) == 1
    assert facts[0]["value"] == "en"


@pytest.mark.asyncio
async def test_recall_facts_with_query(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "city", "Moscow")
    await memory.store_fact("u1", "role", "analyst")
    facts = await memory.recall_facts("u1", query="Mosc")
    assert len(facts) == 1
    assert facts[0]["key"] == "city"


@pytest.mark.asyncio
async def test_clear_facts(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "a", "1")
    await memory.clear_facts("u1")
    assert await memory.recall_facts("u1") == []


@pytest.mark.asyncio
async def test_vacuum_noop_on_facts(memory: SQLiteMemory) -> None:
    await memory.store_fact("u1", "k", "v")
    await memory.vacuum()
    facts = await memory.recall_facts("u1")
    assert len(facts) == 1


@pytest.mark.asyncio
async def test_legacy_tables_dropped_on_init(tmp_path) -> None:
    """B-106/B-108: messages + memory_facts dropped; memory_entries remains."""
    db = tmp_path / "legacy.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                user_id TEXT,
                role TEXT,
                content TEXT
            )
            """
        )
        conn.execute("INSERT INTO messages (user_id, role, content) VALUES ('u', 'user', 'old')")
        conn.execute(
            """
            CREATE TABLE memory_facts (
                id INTEGER PRIMARY KEY,
                user_id TEXT,
                key TEXT,
                value TEXT
            )
            """
        )
        conn.commit()

    mem = SQLiteMemory(str(db))
    with sqlite3.connect(str(mem.db_path)) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "messages" not in tables
    assert "memory_facts" not in tables
    assert "memory_entries" in tables
    # Facts API still works via memory_entries aliases.
    await mem.store_fact("u", "k", "v")
    assert (await mem.recall_facts("u"))[0]["value"] == "v"
