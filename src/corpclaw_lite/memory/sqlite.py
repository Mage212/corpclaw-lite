# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import json
import logging
import sqlite3
from functools import partial
from typing import Any

import anyio

from corpclaw_lite.exceptions import MemoryError
from corpclaw_lite.paths import DATA_DIR
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "SQLiteMemory",
]

logger = logging.getLogger(__name__)

# Data directory: absolute path, supports CORPCLAW_DATA_DIR env var override.
_DATA_DIR = DATA_DIR


class SQLiteMemory:
    """Persistent storage for agent conversation history and facts using SQLite.

    All public methods are async and delegate blocking SQLite I/O to a thread pool
    via ``anyio.to_thread.run_sync`` to avoid blocking the event loop.
    """

    def __init__(self, db_path: str = "memory.db"):
        self.db_path = _DATA_DIR / db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create the necessary tables if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_user
                    ON messages(user_id, timestamp)
                    """
                )
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
                # Migration: add reasoning column if not present
                try:
                    conn.execute("ALTER TABLE messages ADD COLUMN reasoning TEXT")
                    logger.debug("Added 'reasoning' column to messages table")
                except sqlite3.OperationalError:
                    pass  # Column already exists
        except Exception as e:
            logger.critical("Failed to initialize SQLite Memory: %s", e)
            raise MemoryError(f"Database initialization failed: {e}") from e

    # ── Messages ─────────────────────────────────────────────────────────────

    def _sync_add_message(
        self, user_id: str, role: str, content_str: str, reasoning: str | None
    ) -> None:
        try:
            with db_connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO messages (user_id, role, content, reasoning) VALUES (?, ?, ?, ?)",
                    (str(user_id), role, content_str, reasoning),
                )
        except Exception as e:
            raise MemoryError(f"Failed to insert message for user {user_id}: {e}") from e

    async def add_message(
        self,
        user_id: str,
        role: str,
        content: str | dict[str, Any],
        reasoning: str | None = None,
    ) -> None:
        """Add a message to the memory store.

        Args:
            user_id: User identifier.
            role: Message role (user, assistant, system).
            content: Message content (string or JSON-serializable dict).
            reasoning: Optional model reasoning/chain-of-thought (stored for
                audit and future context injection, not returned by get_history).
        """
        content_str = json.dumps(content) if isinstance(content, dict) else str(content)
        await anyio.to_thread.run_sync(
            partial(self._sync_add_message, user_id, role, content_str, reasoning)
        )

    def _sync_get_history(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT role, content FROM messages
                    WHERE user_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (str(user_id), limit),
                )
                rows = cursor.fetchall()

                history: list[dict[str, Any]] = []
                for r in reversed(rows):
                    role = r["role"]
                    content_str = r["content"]
                    history.append({"role": role, "content": content_str})
                return history
        except Exception as e:
            raise MemoryError(f"Failed to fetch history for user {user_id}: {e}") from e

    async def get_history(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Retrieve recent conversation history for a user."""
        return await anyio.to_thread.run_sync(partial(self._sync_get_history, user_id, limit))

    def _sync_clear(self, user_id: str) -> None:
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("DELETE FROM messages WHERE user_id = ?", (str(user_id),))
        except Exception as e:
            raise MemoryError(f"Failed to clear memory for user {user_id}: {e}") from e

    async def clear(self, user_id: str) -> None:
        """Clear the history for a user."""
        await anyio.to_thread.run_sync(partial(self._sync_clear, user_id))

    def _sync_count_messages(self, user_id: str) -> int:
        try:
            with db_connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_id = ?",
                    (str(user_id),),
                )
                row = cursor.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            raise MemoryError(f"Failed to count messages for user {user_id}: {e}") from e

    async def count_messages(self, user_id: str) -> int:
        """Return the total number of messages for a user."""
        return await anyio.to_thread.run_sync(partial(self._sync_count_messages, user_id))

    def _sync_get_oldest_message_ids(self, user_id: str, count: int) -> list[int]:
        try:
            with db_connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE user_id = ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                    """,
                    (str(user_id), count),
                )
                return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            raise MemoryError(f"Failed to get oldest message IDs for user {user_id}: {e}") from e

    async def get_oldest_message_ids(self, user_id: str, count: int) -> list[int]:
        """Return IDs of the N oldest messages for a user."""
        return await anyio.to_thread.run_sync(
            partial(self._sync_get_oldest_message_ids, user_id, count)
        )

    def _sync_replace_oldest(self, user_id: str, count: int, summary: str) -> None:
        """Replace the N oldest messages with a summary in a single atomic transaction."""
        try:
            with db_connect(self.db_path) as conn:
                # SELECT + DELETE + INSERT in one connection = atomic
                cursor = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE user_id = ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                    """,
                    (str(user_id), count),
                )
                ids = [row[0] for row in cursor.fetchall()]
                if not ids:
                    return
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})",  # noqa: S608
                    ids,
                )
                conn.execute(
                    "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                    (str(user_id), "assistant", f"[Conversation summary]: {summary}"),
                )
        except Exception as e:
            raise MemoryError(f"Failed to consolidate messages for user {user_id}: {e}") from e

    async def replace_oldest(self, user_id: str, count: int, summary: str) -> None:
        """Delete the N oldest messages and insert a consolidation summary.

        Runs in a single transaction to avoid data loss.
        """
        await anyio.to_thread.run_sync(partial(self._sync_replace_oldest, user_id, count, summary))

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
            raise MemoryError(f"Failed to store fact for user {user_id}: {e}") from e

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
            raise MemoryError(f"Failed to recall facts for user {user_id}: {e}") from e

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
            raise MemoryError(f"Failed to clear facts for user {user_id}: {e}") from e

    async def clear_facts(self, user_id: str) -> None:
        """Delete all facts for a user."""
        await anyio.to_thread.run_sync(partial(self._sync_clear_facts, user_id))
