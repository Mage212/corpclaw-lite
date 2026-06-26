"""Tests for the shared SQLite ``db_connect`` helper — busy_timeout behaviour."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from corpclaw_lite.utils.db import db_connect


def test_db_connect_sets_busy_timeout(tmp_path: Path) -> None:
    """db_connect must set PRAGMA busy_timeout=5000 on every connection.

    Regression guard for the multi-user fix: without busy_timeout, concurrent writers
    on the shared WAL file raise ``database is locked`` immediately instead of waiting.
    """
    db_path = tmp_path / "test.db"
    with db_connect(db_path) as conn:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row is not None
        assert row[0] == 5000


def test_db_connect_concurrent_writers_do_not_lock(tmp_path: Path) -> None:
    """Two threads writing through db_connect must not raise ``database is locked``.

    With busy_timeout=5000 the second writer waits for the writer lock; previously
    (busy_timeout=0) it failed immediately.
    """
    db_path = tmp_path / "concurrent.db"
    # Schema needs to exist before concurrent inserts.
    with db_connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")

    errors: list[BaseException] = []

    def writer(thread_id: int) -> None:
        try:
            for i in range(20):
                with db_connect(db_path) as conn:
                    conn.execute("INSERT INTO t (v) VALUES (?)", (f"{thread_id}-{i}",))
        except BaseException as exc:  # noqa: BLE001 — capture any failure
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(tid,)) for tid in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent writes failed: {errors}"

    with db_connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert count == 4 * 20

    # Sanity: OperationalError("database is locked") must not appear anywhere.
    for exc in errors:
        assert not isinstance(exc, sqlite3.OperationalError)
