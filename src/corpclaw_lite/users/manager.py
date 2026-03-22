from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)


class UserManager:
    """
    Manages user storage in SQLite.
    Users are stored in the same DB as memory (data/memory.db by default).
    """

    def __init__(self, db_path: str = "data/users.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE,
                    name TEXT NOT NULL DEFAULT '',
                    department TEXT NOT NULL DEFAULT 'default',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def create_user(
        self,
        telegram_id: int,
        department: str,
        name: str = "",
    ) -> User:
        """Insert a new user and return it."""
        with sqlite3.connect(self._db) as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO users (telegram_id, name, department) VALUES (?,?,?)",
                (telegram_id, name or f"user_{telegram_id}", department),
            )
            row_id = cursor.lastrowid or self._get_id_by_telegram(telegram_id)
        return User(id=row_id, name=name or f"user_{telegram_id}", department=department,
                    telegram_id=telegram_id)

    def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Look up a user by their Telegram ID."""
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        if not row:
            return None
        return User(
            id=row["id"],
            name=row["name"],
            department=row["department"],
            telegram_id=row["telegram_id"],
        )

    def list_users(self) -> list[User]:
        """Return all registered users."""
        with sqlite3.connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [
            User(
                id=r["id"],
                name=r["name"],
                department=r["department"],
                telegram_id=r["telegram_id"],
            )
            for r in rows
        ]

    def _get_id_by_telegram(self, telegram_id: int) -> int:
        with sqlite3.connect(self._db) as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return int(row[0]) if row else 0
