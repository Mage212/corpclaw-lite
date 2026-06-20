"""Tests for B-040: FileChangeDAO — file-change journal."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from corpclaw_lite.memory.file_changes import FileChangeDAO


@pytest.fixture
def dao(tmp_path: Path) -> FileChangeDAO:
    return FileChangeDAO(db_path=tmp_path / "test.db")


# ─── schema ──────────────────────────────────────────────────────────────────


def test_init_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    FileChangeDAO(db_path=db)
    assert db.exists()
    import sqlite3

    with sqlite3.connect(db) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "agent_change_sets" in tables
    assert "agent_file_changes" in tables


# ─── record_change ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_change_inserts_set_and_change(dao: FileChangeDAO) -> None:
    change_id = await dao.record_change(
        user_id="user1",
        run_id="run1",
        tool_name="write_file",
        file_path="report.txt",
        op="modify",
        before_hash="aaa",
        after_hash="bbb",
        backup_path="report.txt",
        size_bytes=100,
    )
    assert change_id == "run1:0"
    changes = await dao.list_for_run("run1")
    assert len(changes) == 1
    c = changes[0]
    assert c.run_id == "run1"
    assert c.tool_name == "write_file"
    assert c.op == "modify"
    assert c.before_hash == "aaa"
    assert c.after_hash == "bbb"
    assert c.backup_path == "report.txt"
    assert c.size_bytes == 100
    assert c.status == "open"


@pytest.mark.asyncio
async def test_record_change_hash_dedup_skips_noop(dao: FileChangeDAO) -> None:
    """before_hash == after_hash → no-op, returns None, no row written."""
    change_id = await dao.record_change(
        user_id="u",
        run_id="r",
        tool_name="write_file",
        file_path="f.txt",
        op="modify",
        before_hash="same",
        after_hash="same",
        backup_path=None,
        size_bytes=0,
    )
    assert change_id is None
    changes = await dao.list_for_run("r")
    assert changes == []


@pytest.mark.asyncio
async def test_record_change_create_op_has_null_before_hash(dao: FileChangeDAO) -> None:
    change_id = await dao.record_change(
        user_id="u",
        run_id="r",
        tool_name="normalize_excel",
        file_path="out.xlsx",
        op="create",
        before_hash=None,
        after_hash="abc",
        backup_path=None,
        size_bytes=42,
    )
    assert change_id is not None
    changes = await dao.list_for_run("r")
    assert changes[0].op == "create"
    assert changes[0].before_hash is None


@pytest.mark.asyncio
async def test_record_change_sort_order_increments(dao: FileChangeDAO) -> None:
    for i in range(3):
        await dao.record_change(
            user_id="u",
            run_id="r",
            tool_name="write_file",
            file_path=f"f{i}.txt",
            op="modify",
            before_hash=f"a{i}",
            after_hash=f"b{i}",
            backup_path=None,
            size_bytes=1,
        )
    changes = await dao.list_for_run("r")
    assert [c.sort_order for c in changes] == [0, 1, 2]
    assert [c.id for c in changes] == ["r:0", "r:1", "r:2"]


@pytest.mark.asyncio
async def test_record_change_upserts_change_set(dao: FileChangeDAO) -> None:
    """Multiple changes in the same run share one change_set row."""
    await dao.record_change(
        user_id="u",
        run_id="r",
        tool_name="t",
        file_path="a",
        op="modify",
        before_hash="x",
        after_hash="y",
        backup_path=None,
        size_bytes=1,
    )
    await dao.record_change(
        user_id="u",
        run_id="r",
        tool_name="t",
        file_path="b",
        op="modify",
        before_hash="x",
        after_hash="z",
        backup_path=None,
        size_bytes=1,
    )
    import sqlite3

    with sqlite3.connect(dao.db_path) as conn:
        rows = conn.execute(
            "SELECT run_id, status FROM agent_change_sets WHERE run_id=?", ("r",)
        ).fetchall()
    assert len(rows) == 1  # UPSERT, not insert
    assert rows[0][1] == "open"


# ─── list_recent_for_user ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_recent_for_user_orders_by_created_desc(dao: FileChangeDAO) -> None:
    for i in range(5):
        await dao.record_change(
            user_id="u",
            run_id=f"r{i}",
            tool_name="t",
            file_path=f"f{i}.txt",
            op="modify",
            before_hash="x",
            after_hash=f"y{i}",
            backup_path=None,
            size_bytes=1,
        )
        time.sleep(0.005)  # distinct created_at
    recent = await dao.list_recent_for_user("u", limit=3)
    assert len(recent) == 3
    # Most recent first
    assert recent[0].created_at >= recent[-1].created_at


@pytest.mark.asyncio
async def test_list_recent_for_user_filters_by_user(dao: FileChangeDAO) -> None:
    await dao.record_change(
        user_id="u1",
        run_id="r1",
        tool_name="t",
        file_path="a",
        op="modify",
        before_hash="x",
        after_hash="y",
        backup_path=None,
        size_bytes=1,
    )
    await dao.record_change(
        user_id="u2",
        run_id="r2",
        tool_name="t",
        file_path="b",
        op="modify",
        before_hash="x",
        after_hash="y",
        backup_path=None,
        size_bytes=1,
    )
    recent = await dao.list_recent_for_user("u1", limit=5)
    assert len(recent) == 1
    assert recent[0].user_id == "u1"


# ─── mark_reverted + recompute_run_status ────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_reverted_and_recompute_status(dao: FileChangeDAO) -> None:
    """Reverting all changes in a run marks the run as reverted."""
    for i in range(2):
        await dao.record_change(
            user_id="u",
            run_id="r",
            tool_name="t",
            file_path=f"f{i}",
            op="modify",
            before_hash="x",
            after_hash=f"y{i}",
            backup_path=None,
            size_bytes=1,
        )
    # Revert only first → run still open
    ok = await dao.mark_reverted("r", "r:0")
    assert ok is True
    import sqlite3

    with sqlite3.connect(dao.db_path) as conn:
        row = conn.execute("SELECT status FROM agent_change_sets WHERE run_id=?", ("r",)).fetchone()
    assert row[0] == "open"
    # Revert second → run now reverted
    ok = await dao.mark_reverted("r", "r:1")
    assert ok is True
    with sqlite3.connect(dao.db_path) as conn:
        row = conn.execute("SELECT status FROM agent_change_sets WHERE run_id=?", ("r",)).fetchone()
    assert row[0] == "reverted"


@pytest.mark.asyncio
async def test_mark_reverted_idempotent(dao: FileChangeDAO) -> None:
    await dao.record_change(
        user_id="u",
        run_id="r",
        tool_name="t",
        file_path="f",
        op="modify",
        before_hash="x",
        after_hash="y",
        backup_path=None,
        size_bytes=1,
    )
    assert await dao.mark_reverted("r", "r:0") is True
    # Already reverted → False (no row updated)
    assert await dao.mark_reverted("r", "r:0") is False


# ─── GC ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gc_finalized_before_deletes_old_reverted(dao: FileChangeDAO) -> None:
    await dao.record_change(
        user_id="u",
        run_id="r",
        tool_name="t",
        file_path="f",
        op="modify",
        before_hash="x",
        after_hash="y",
        backup_path=None,
        size_bytes=1,
    )
    await dao.mark_reverted("r", "r:0")
    # GC with future cutoff → deletes
    deleted = await dao.gc_finalized_before(cutoff_ms=int(time.time() * 1000) + 1000)
    assert "r" in deleted
    assert await dao.list_for_run("r") == []


@pytest.mark.asyncio
async def test_gc_keeps_open_runs(dao: FileChangeDAO) -> None:
    await dao.record_change(
        user_id="u",
        run_id="r",
        tool_name="t",
        file_path="f",
        op="modify",
        before_hash="x",
        after_hash="y",
        backup_path=None,
        size_bytes=1,
    )
    deleted = await dao.gc_finalized_before(cutoff_ms=int(time.time() * 1000) + 1000)
    assert deleted == []  # open run is kept
    assert await dao.list_for_run("r") != []
