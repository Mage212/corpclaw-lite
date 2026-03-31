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
    """
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
