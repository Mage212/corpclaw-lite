"""Tests for SQLiteMemory entries API (B-106 facts slim-down + B-108 Memora)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from corpclaw_lite.memory.sqlite import _CUE_EXACT_WEIGHT, SQLiteMemory


def _fts_count(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM memory_entries_fts").fetchone()[0])


def _entries_count(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0])


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


@pytest.mark.asyncio
async def test_fts_survives_reinit(tmp_path: Path) -> None:
    """M1: re-opening SQLiteMemory must not wipe a populated FTS index."""
    db = tmp_path / "durable.db"
    m1 = SQLiteMemory(str(db))
    await m1.store_entry(
        "u1",
        primary_abstraction="Client INN for Acme",
        memory_value="7707083893",
        cues=["Acme", "7707083893"],
    )
    assert _entries_count(m1.db_path) == 1
    assert _fts_count(m1.db_path) == 1

    m2 = SQLiteMemory(str(db))
    assert _entries_count(m2.db_path) == 1
    assert _fts_count(m2.db_path) == 1
    hits = await m2.recall_entries("u1", "Acme")
    assert len(hits) >= 1
    assert hits[0]["abstraction"] == "Client INN for Acme"
    # FTS+BM25 path scores above pure cue-only (1.0) when index is warm.
    assert float(hits[0]["score"]) > _CUE_EXACT_WEIGHT


@pytest.mark.asyncio
async def test_fts_rebuild_when_empty(tmp_path: Path) -> None:
    """M1: empty/desynced FTS is rebuilt from memory_entries on init."""
    db = tmp_path / "rebuild.db"
    m1 = SQLiteMemory(str(db))
    await m1.store_entry(
        "u1",
        primary_abstraction="Prefers brief replies",
        memory_value="short answers",
        cues=["brief"],
    )
    await m1.store_entry(
        "u1",
        primary_abstraction="Works in Moscow",
        memory_value="office",
        cues=["Moscow"],
    )
    assert _fts_count(m1.db_path) == 2

    with sqlite3.connect(str(m1.db_path)) as conn:
        conn.execute("DELETE FROM memory_entries_fts")
        conn.commit()
    assert _fts_count(m1.db_path) == 0
    assert _entries_count(m1.db_path) == 2

    m2 = SQLiteMemory(str(db))
    assert _fts_count(m2.db_path) == 2
    assert _entries_count(m2.db_path) == 2
    hits = await m2.recall_entries("u1", "brief")
    assert any(h["abstraction"] == "Prefers brief replies" for h in hits)


@pytest.mark.asyncio
async def test_cue_exact_score_not_doubled(tmp_path: Path) -> None:
    """S2: exact-cue boost applied once in FTS path (no all-rows double-count)."""
    db = tmp_path / "score.db"
    m = SQLiteMemory(str(db))
    await m.store_entry(
        "u1",
        primary_abstraction="Tax id Acme",
        memory_value="x",
        cues=["Acme"],
    )
    hits = await m.recall_entries("u1", "Acme")
    assert len(hits) == 1
    score = float(hits[0]["score"])
    # Double-boost historically produced ~12; single boost stays strictly below that.
    assert score < 12.0
    # Still above pure cue-only floor when FTS MATCH hits.
    assert score > _CUE_EXACT_WEIGHT


# ── S3-14: memory value bound + fallback recall LIMIT ─────────────────────────


@pytest.mark.asyncio
async def test_store_entry_truncates_oversized_value(memory: SQLiteMemory) -> None:
    """An oversized memory_value is truncated to the cap, not stored unbounded."""
    from corpclaw_lite.memory.sqlite import _MAX_VALUE_LEN

    big = "y" * (_MAX_VALUE_LEN + 500)
    await memory.store_entry("u1", primary_abstraction="big fact", memory_value=big, cues=[])

    import sqlite3

    with sqlite3.connect(str(memory.db_path)) as conn:
        stored = conn.execute(
            "SELECT memory_value FROM memory_entries WHERE user_id = ?", ("u1",)
        ).fetchone()
    assert stored is not None
    assert len(stored[0]) == _MAX_VALUE_LEN


def test_fallback_recall_limit_constant_is_bounded() -> None:
    """The fallback full-scan is capped so a user with many entries is bounded."""
    from corpclaw_lite.memory.sqlite import _FALLBACK_RECALL_LIMIT

    assert _FALLBACK_RECALL_LIMIT <= 1000


# ── S1-09: legacy DROP migration gate ──────────────────────────────────────


def test_legacy_drop_runs_once_then_marker_prevents_re_drop(tmp_path: Path) -> None:
    """S1-09: the legacy messages/memory_facts DROP runs exactly once per DB.

    First init drops legacy tables and sets the marker; a second init must NOT
    re-run the DROPs (a second SQLiteMemory instance / CLI subcommand / restart
    during an in-flight writer must not wipe live data).
    """
    db_file = tmp_path / "mem.db"

    # Seed a DB with legacy tables + a marker that is ABSENT, simulating a
    # pre-S1-09 database that still has messages/memory_facts.
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER)")
        conn.execute("CREATE TABLE memory_facts (id INTEGER)")
        conn.execute("INSERT INTO memory_facts VALUES (42)")
        conn.execute(
            "CREATE TABLE memory_entries (id INTEGER PRIMARY KEY, user_id TEXT,"
            " primary_abstraction TEXT, memory_value TEXT,"
            " cue_indices_json TEXT DEFAULT '[]', created_at TIMESTAMP,"
            " updated_at TIMESTAMP, UNIQUE(user_id, primary_abstraction))"
        )
        conn.execute("INSERT INTO memory_entries VALUES (1,'u','name','Alice','[]','t','t')")
        conn.commit()

    # First init: legacy tables dropped, marker set.
    SQLiteMemory(str(db_file))
    with sqlite3.connect(str(db_file)) as conn:
        # Legacy tables gone.
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_facts'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
            ).fetchone()
            is None
        )
        # Marker present.
        assert (
            conn.execute("SELECT 1 FROM app_metadata WHERE key='memory_legacy_drop_v1'").fetchone()
            is not None
        )
        # memory_entries survived.
        assert conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 1

    # Second init: marker present → no re-drop attempt (and no error).
    SQLiteMemory(str(db_file))
    with sqlite3.connect(str(db_file)) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_facts'"
            ).fetchone()
            is None
        )


def test_fresh_db_sets_marker_without_legacy_tables(tmp_path: Path) -> None:
    """S1-09: a brand-new DB (no legacy tables) still gets the marker on first init."""
    db_file = tmp_path / "fresh.db"
    SQLiteMemory(str(db_file))
    with sqlite3.connect(str(db_file)) as conn:
        assert (
            conn.execute("SELECT 1 FROM app_metadata WHERE key='memory_legacy_drop_v1'").fetchone()
            is not None
        )
        # memory_entries created normally.
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_entries'"
            ).fetchone()
            is not None
        )


@pytest.mark.asyncio
async def test_store_recall_survives_reinit(tmp_path: Path) -> None:
    """S1-09 regression: facts stored before a re-init survive (no data loss)."""
    db_file = tmp_path / "persist.db"
    m1 = SQLiteMemory(str(db_file))
    await m1.store_fact("u1", "role", "analyst")
    assert await m1.recall_facts("u1", "role")

    # A new instance on the same DB file — must not drop anything.
    m2 = SQLiteMemory(str(db_file))
    facts = await m2.recall_facts("u1", "role")
    assert facts
    assert any("analyst" in str(f) for f in facts)
