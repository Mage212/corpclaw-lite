from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SQLiteMemory:
    """Persistent storage for agent conversation history and facts using SQLite."""

    def __init__(self, db_path: str = "memory.db"):
        self.db_path = Path("data") / db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create the necessary tables if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(self.db_path) as conn:
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
        except Exception as e:
            logger.error("Failed to initialize SQLite Memory: %s", e)

    def add_message(self, user_id: str, role: str, content: str | dict[str, Any]) -> None:
        """Add a message to the memory store."""
        content_str = json.dumps(content) if isinstance(content, dict) else str(content)

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                    (str(user_id), role, content_str),
                )
        except Exception as e:
            logger.error("Failed to insert message into memory for user %s: %s", user_id, e)

    def get_history(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Retrieve recent conversation history for a user."""
        try:
            with sqlite3.connect(self.db_path) as conn:
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

                # Fetched DESC (newest first) → reverse to chronological order.
                history: list[dict[str, Any]] = []
                for r in reversed(rows):
                    role = r["role"]
                    content_str = r["content"]

                    try:
                        content: Any = json.loads(content_str)
                    except json.JSONDecodeError:
                        content = content_str

                    history.append({"role": role, "content": content})

                return history
        except Exception as e:
            logger.error("Failed to fetch history for user %s: %s", user_id, e)
            return []

    def clear(self, user_id: str) -> None:
        """Clear the history for a user."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM messages WHERE user_id = ?", (str(user_id),))
        except Exception as e:
            logger.error("Failed to clear memory for user %s: %s", user_id, e)

    def count_messages(self, user_id: str) -> int:
        """Return the total number of messages for a user."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_id = ?",
                    (str(user_id),),
                )
                row = cursor.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            logger.error("Failed to count messages for user %s: %s", user_id, e)
            return 0

    def get_oldest_message_ids(self, user_id: str, count: int) -> list[int]:
        """Return IDs of the N oldest messages for a user."""
        try:
            with sqlite3.connect(self.db_path) as conn:
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
            logger.error("Failed to get oldest message IDs for user %s: %s", user_id, e)
            return []

    def replace_oldest(self, user_id: str, count: int, summary: str) -> None:
        """Delete the N oldest messages and insert a consolidation summary.

        Runs in a single transaction to avoid data loss.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                ids = self.get_oldest_message_ids(user_id, count)
                if not ids:
                    return
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})",  # noqa: S608
                    ids,
                )
                conn.execute(
                    "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                    (str(user_id), "system", f"[Conversation summary]: {summary}"),
                )
        except Exception as e:
            logger.error("Failed to consolidate messages for user %s: %s", user_id, e)

    # ── Fact storage ─────────────────────────────────────────────────────────

    def store_fact(self, user_id: str, key: str, value: str) -> None:
        """Upsert a key-value fact for a user."""
        try:
            with sqlite3.connect(self.db_path) as conn:
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
            logger.error("Failed to store fact for user %s: %s", user_id, e)

    def recall_facts(
        self, user_id: str, query: str | None = None, limit: int = 10
    ) -> list[dict[str, str]]:
        """Recall facts for a user, optionally filtered by LIKE search on key and value."""
        try:
            with sqlite3.connect(self.db_path) as conn:
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
            logger.error("Failed to recall facts for user %s: %s", user_id, e)
            return []

    def clear_facts(self, user_id: str) -> None:
        """Delete all facts for a user."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM memory_facts WHERE user_id = ?", (str(user_id),))
        except Exception as e:
            logger.error("Failed to clear facts for user %s: %s", user_id, e)
