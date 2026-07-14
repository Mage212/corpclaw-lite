# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import logging
import sqlite3
from functools import partial

import anyio

from corpclaw_lite.exceptions import StorageError
from corpclaw_lite.paths import DATA_DIR
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "SQLiteMemory",
]

logger = logging.getLogger(__name__)

_DATA_DIR = DATA_DIR


class SQLiteMemory:
    """Cross-chat structured facts store (user_id-keyed), backed by SQLite.

    B-106 / D-078: transcript history lives exclusively in ``ChatContextStore``.
    This class no longer owns a ``messages`` table — only ``memory_facts``
    (used by memory_store/recall tools, onboarding finalizer, and loop fact recall).

    Class name ``SQLiteMemory`` is kept for import stability; a rename to
    ``MemoryFactsStore`` is optional follow-up, not part of this slim-down.

    All public methods are async and delegate blocking SQLite I/O to a thread pool
    via ``anyio.to_thread.run_sync`` to avoid blocking the event loop.
    """

    def __init__(self, db_path: str = "memory.db"):
        self.db_path = _DATA_DIR / db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create facts tables; drop legacy messages table if present (B-106)."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                # B-106: transcript is ChatContextStore-only. Drop legacy table so
                # old text-only history cannot be read by accident (clean start, D-078).
                conn.execute("DROP TABLE IF EXISTS messages")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_facts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, key)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_facts_user
                    ON memory_facts(user_id)
                    """
                )
        except Exception as e:
            logger.critical("Failed to initialize SQLite Memory: %s", e)
            raise StorageError(f"Database initialization failed: {e}") from e

    # ── Vacuum ──────────────────────────────────────────────────────────────

    def _sync_vacuum(self) -> None:
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            logger.info("SQLite vacuum completed for %s", self.db_path)
        except Exception as e:
            logger.warning("SQLite vacuum failed: %s", e)

    async def vacuum(self) -> None:
        """Manually trigger VACUUM + WAL checkpoint to reclaim disk space."""
        await anyio.to_thread.run_sync(partial(self._sync_vacuum))

    # ── Fact storage ─────────────────────────────────────────────────────────

    def _sync_store_fact(self, user_id: str, key: str, value: str) -> None:
        try:
            with db_connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO memory_facts (user_id, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, key)
                    DO UPDATE SET value = excluded.value,
                                  created_at = CURRENT_TIMESTAMP
                    """,
                    (str(user_id), key, value),
                )
        except Exception as e:
            raise StorageError(f"Failed to store fact for user {user_id}: {e}") from e

    async def store_fact(self, user_id: str, key: str, value: str) -> None:
        """Upsert a key-value fact for a user."""
        await anyio.to_thread.run_sync(partial(self._sync_store_fact, user_id, key, value))

    def _sync_recall_facts(
        self, user_id: str, query: str | None, limit: int
    ) -> list[dict[str, str]]:
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if query:
                    like = f"%{query}%"
                    cursor = conn.execute(
                        """
                        SELECT key, value FROM memory_facts
                        WHERE user_id = ? AND (key LIKE ? OR value LIKE ?)
                        ORDER BY created_at DESC LIMIT ?
                        """,
                        (str(user_id), like, like, limit),
                    )
                else:
                    cursor = conn.execute(
                        """
                        SELECT key, value FROM memory_facts
                        WHERE user_id = ?
                        ORDER BY created_at DESC LIMIT ?
                        """,
                        (str(user_id), limit),
                    )
                return [{"key": r["key"], "value": r["value"]} for r in cursor.fetchall()]
        except Exception as e:
            raise StorageError(f"Failed to recall facts for user {user_id}: {e}") from e

    async def recall_facts(
        self, user_id: str, query: str | None = None, limit: int = 10
    ) -> list[dict[str, str]]:
        """Recall facts for a user, optionally filtered by LIKE search on key and value."""
        return await anyio.to_thread.run_sync(
            partial(self._sync_recall_facts, user_id, query, limit)
        )

    def _sync_clear_facts(self, user_id: str) -> None:
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("DELETE FROM memory_facts WHERE user_id = ?", (str(user_id),))
        except Exception as e:
            raise StorageError(f"Failed to clear facts for user {user_id}: {e}") from e

    async def clear_facts(self, user_id: str) -> None:
        """Delete all facts for a user."""
        await anyio.to_thread.run_sync(partial(self._sync_clear_facts, user_id))
