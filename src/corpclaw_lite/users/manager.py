# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import json
import logging
import sqlite3
from functools import partial
from pathlib import Path

import anyio

from corpclaw_lite.users.models import User
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "UserManager",
]

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
        self._whitelist_path = self._db.parent / "whitelist.json"
        self._revoked_path = self._db.parent / "revoked_sessions.json"
        self._whitelist_cache: list[dict[str, int | str]] | None = None
        self._revoked_cache: set[int] | None = None

    def _init_db(self) -> None:
        with db_connect(self._db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
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
        """Insert a new user (if not exists) and return the DB record."""
        with db_connect(self._db) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (telegram_id, name, department) VALUES (?,?,?)",
                (telegram_id, name or f"user_{telegram_id}", department),
            )
        # Always read back the actual DB state (handles duplicate insert gracefully)
        existing = self.get_by_telegram_id(telegram_id)
        if existing:
            return existing
        return User(
            id=0,
            name=name or f"user_{telegram_id}",
            department=department,
            telegram_id=telegram_id,
        )

    def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Look up a user by their Telegram ID."""
        with db_connect(self._db) as conn:
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
        with db_connect(self._db) as conn:
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
        with db_connect(self._db) as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    # ── Whitelist ─────────────────────────────────────────────────────────────

    def _load_whitelist(self) -> list[dict[str, int | str]]:
        """Load whitelist entries from JSON file (cached)."""
        if self._whitelist_cache is not None:
            return self._whitelist_cache
        if not self._whitelist_path.exists():
            return []
        try:
            data = json.loads(self._whitelist_path.read_text("utf-8"))
            if isinstance(data, list):
                self._whitelist_cache = data  # type: ignore[assignment]
                return data  # type: ignore[return-value]
        except Exception as e:
            logger.warning("Failed to load whitelist: %s", e)
        return []

    def _save_whitelist(self, entries: list[dict[str, int | str]]) -> None:
        """Save whitelist entries to JSON file (atomic write) and update cache."""
        self._whitelist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._whitelist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        tmp.replace(self._whitelist_path)
        self._whitelist_cache = entries

    def seed_whitelist(self, telegram_ids: list[int], default_department: str) -> None:
        """Merge config-based whitelist IDs into the persistent file.

        Only adds IDs that are not already present. Called once at startup.
        """
        entries = self._load_whitelist()
        existing_ids = {e["telegram_id"] for e in entries}
        added = 0
        for tid in telegram_ids:
            if tid not in existing_ids:
                entries.append({"telegram_id": tid, "department": default_department})
                added += 1
        if added:
            self._save_whitelist(entries)
            logger.info("Seeded %d IDs into whitelist", added)

    def add_to_whitelist(self, telegram_id: int, department: str = "default") -> None:
        """Add a telegram_id to the persistent whitelist."""
        entries = self._load_whitelist()
        existing_ids = {e["telegram_id"] for e in entries}
        if telegram_id in existing_ids:
            logger.info("telegram_id=%d already in whitelist", telegram_id)
            return
        entries.append({"telegram_id": telegram_id, "department": department})
        self._save_whitelist(entries)
        logger.info("Added telegram_id=%d to whitelist (dept=%s)", telegram_id, department)

    def remove_from_whitelist(self, telegram_id: int) -> bool:
        """Remove a telegram_id from the persistent whitelist. Returns True if found."""
        entries = self._load_whitelist()
        new_entries = [e for e in entries if e.get("telegram_id") != telegram_id]
        if len(new_entries) == len(entries):
            return False
        self._save_whitelist(new_entries)
        logger.info("Removed telegram_id=%d from whitelist", telegram_id)
        return True

    def get_whitelist(self) -> list[dict[str, int | str]]:
        """Return the full whitelist."""
        return self._load_whitelist()

    def is_allowed(self, telegram_id: int) -> bool:
        """Check if telegram_id is in the whitelist (deny-by-default)."""
        entries = self._load_whitelist()
        if not entries:
            return False  # deny all when empty
        return any(e.get("telegram_id") == telegram_id for e in entries)

    def get_whitelist_department(self, telegram_id: int) -> str:
        """Return department for a whitelisted telegram_id, or 'default'."""
        for e in self._load_whitelist():
            if e.get("telegram_id") == telegram_id:
                dept = e.get("department", "default")
                return str(dept)
        return "default"

    # ── Revoked Sessions ──────────────────────────────────────────────────────

    def _load_revoked(self) -> set[int]:
        if self._revoked_cache is not None:
            return self._revoked_cache
        if not self._revoked_path.exists():
            return set()
        try:
            data = json.loads(self._revoked_path.read_text("utf-8"))
            if isinstance(data, list):
                result = {int(item) for item in data if isinstance(item, (int, float, str))}  # type: ignore[misc]
                self._revoked_cache = result
                return result
        except Exception as e:
            logger.warning("Failed to load revoked sessions: %s", e)
        return set()

    def _save_revoked(self, revoked: set[int]) -> None:
        self._revoked_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._revoked_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(revoked)), encoding="utf-8")
        tmp.replace(self._revoked_path)
        self._revoked_cache = revoked

    def revoke_session(self, telegram_id: int) -> None:
        """Block a user from interacting with the bot."""
        revoked = self._load_revoked()
        revoked.add(telegram_id)
        self._save_revoked(revoked)
        logger.info("Session revoked for telegram_id=%d", telegram_id)

    def unrevoke_session(self, telegram_id: int) -> None:
        """Unblock a previously revoked user."""
        revoked = self._load_revoked()
        revoked.discard(telegram_id)
        self._save_revoked(revoked)

    def is_session_revoked(self, telegram_id: int) -> bool:
        """Check if a user's session is revoked."""
        return telegram_id in self._load_revoked()

    # ── Async wrappers (for use from event loop) ─────────────────────────────

    async def async_get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Async wrapper around get_by_telegram_id (runs SQLite in thread)."""
        return await anyio.to_thread.run_sync(partial(self.get_by_telegram_id, telegram_id))

    async def async_create_user(self, telegram_id: int, department: str, name: str = "") -> User:
        """Async wrapper around create_user (runs SQLite in thread)."""
        return await anyio.to_thread.run_sync(
            partial(self.create_user, telegram_id=telegram_id, department=department, name=name)
        )

    def update_name(self, telegram_id: int, name: str) -> None:
        """Update user display name (e.g. after onboarding)."""
        with db_connect(self._db) as conn:
            conn.execute(
                "UPDATE users SET name = ? WHERE telegram_id = ?",
                (name, telegram_id),
            )
        logger.info("Updated name for telegram_id=%d: %s", telegram_id, name)

    async def async_update_name(self, telegram_id: int, name: str) -> None:
        """Async wrapper around update_name."""
        await anyio.to_thread.run_sync(partial(self.update_name, telegram_id, name))
