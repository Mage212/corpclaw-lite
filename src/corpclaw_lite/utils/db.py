"""Shared SQLite utilities."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["db_connect"]


@contextmanager
def db_connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Open a SQLite connection with explicit close on exit.

    Unlike plain ``with sqlite3.connect()`` (which only manages transactions),
    this helper calls ``.close()`` — preventing ResourceWarning in Python 3.12+.

    A ``busy_timeout`` of 5s is set on every connection so concurrent writers on a
    shared WAL file (e.g. the shared ``memory.db`` hit by SQLiteMemory,
    FileChangeDAO and WebChatStore) wait briefly for the writer lock instead of
    raising ``database is locked`` immediately.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
