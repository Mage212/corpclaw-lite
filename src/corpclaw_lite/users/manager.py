# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import json
import logging
import secrets
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import pbkdf2_hmac
from pathlib import Path

import anyio

from corpclaw_lite.users.models import User
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "UserManager",
    "tone_directive",
]

logger = logging.getLogger(__name__)

# Response-tone directives injected into the system prompt per the user's
# ``user_agent_context.tone`` setting (Etap 5). ``"default"`` has no directive —
# the base SOUL.md tone line applies unchanged. ``tone_directive()`` is the
# single point consumed by both ``AgentRequestService`` (run) and the web
# preview handler, so the tone setting can never silently go unused.
_TONE_DIRECTIVES: dict[str, str] = {
    "concise": (
        "Be concise: give short, direct answers. Skip preamble, hedging, and "
        "restating the question. Prefer lists over prose when several items are involved."
    ),
    "detailed": (
        "Be thorough: explain your reasoning, structure the answer with headings, "
        "and include relevant context. Prefer completeness over brevity."
    ),
}


def tone_directive(tone: str) -> str:
    """Return the system-prompt directive for a response tone.

    Returns an empty string for ``"default"`` and any unknown value, so callers
    can always append the result without a special-case branch.
    """
    return _TONE_DIRECTIVES.get(tone, "")


_PASSWORD_ITERATIONS = 200_000
_SESSION_TOKEN_BYTES = 32
_PASSWORD_MIN_LENGTH = 12
_PASSWORD_MAX_LENGTH = 256


class UserManager:
    """
    Manages user storage in SQLite.
    Users are stored in the same DB as memory (data/memory.db by default).
    """

    def __init__(
        self,
        db_path: str = "data/users.db",
        *,
        password_min_length: int = _PASSWORD_MIN_LENGTH,
        password_max_length: int = _PASSWORD_MAX_LENGTH,
    ) -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._password_min_length = max(1, password_min_length)
        self._password_max_length = max(self._password_min_length, password_max_length)
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
                    username TEXT UNIQUE,
                    password_hash TEXT,
                    name TEXT NOT NULL DEFAULT '',
                    department TEXT NOT NULL DEFAULT 'default',
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for column, ddl in (
                ("username", "ALTER TABLE users ADD COLUMN username TEXT"),
                ("password_hash", "ALTER TABLE users ADD COLUMN password_hash TEXT"),
                ("is_admin", "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"),
                ("disabled", "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"),
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        logger.debug("User migration skipped for %s: %s", column, e)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
                "ON users(username) WHERE username IS NOT NULL"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    csrf_token TEXT NOT NULL,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
            # Etap 5: per-user agent context (personal instructions + tone).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_agent_context (
                    user_id INTEGER PRIMARY KEY,
                    instructions TEXT NOT NULL DEFAULT '',
                    tone TEXT NOT NULL DEFAULT 'default',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def create_user(
        self,
        department: str,
        name: str = "",
        telegram_id: int | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        is_admin: bool = False,
    ) -> User:
        """Insert a canonical user and return the DB record."""
        clean_username = self.normalize_username(username) if username is not None else None
        password_hash = None
        if clean_username is not None:
            self._validate_password(password or "")
            password_hash = self.hash_password(str(password))

        if telegram_id is not None:
            existing = self.get_by_telegram_id(telegram_id)
            if existing:
                return existing

        with db_connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO users
                    (telegram_id, username, password_hash, name, department, is_admin)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    clean_username,
                    password_hash,
                    name or clean_username or (f"user_{telegram_id}" if telegram_id else "user"),
                    department,
                    1 if is_admin else 0,
                ),
            )
            user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        user = self.get_by_id(user_id)
        if user is None:
            raise RuntimeError("Failed to create user")
        return user

    @staticmethod
    def normalize_username(username: str) -> str:
        """Normalize and validate a local web username."""
        clean_username = username.strip().lower()
        if not clean_username:
            raise ValueError("username is required")
        if len(clean_username) > 64:
            raise ValueError("username must be 64 characters or fewer")
        if not all(c.isalnum() or c in {"_", "-", "."} for c in clean_username):
            raise ValueError("username may contain only letters, digits, '.', '_' and '-'")
        return clean_username

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a local-account password using PBKDF2-HMAC-SHA256."""
        salt = secrets.token_hex(16)
        digest = pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("ascii"),
            _PASSWORD_ITERATIONS,
        ).hex()
        return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${salt}${digest}"

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """Return True when password matches the stored PBKDF2 hash."""
        try:
            scheme, iterations_raw, salt, expected = password_hash.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            iterations = int(iterations_raw)
            digest = pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt.encode("ascii"),
                iterations,
            ).hex()
            return secrets.compare_digest(digest, expected)
        except Exception:
            return False

    def create_web_user(
        self,
        username: str,
        password: str,
        department: str,
        name: str = "",
        *,
        is_admin: bool = False,
        telegram_id: int | None = None,
    ) -> User:
        """Create a web-only user or attach web credentials to an existing Telegram user."""
        if telegram_id is not None:
            return self.link_web_user(
                telegram_id=telegram_id,
                username=username,
                password=password,
                is_admin=is_admin,
            )

        clean_username = self.normalize_username(username)
        self._validate_password(password)
        password_hash = self.hash_password(password)
        with db_connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, name, department, is_admin)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    clean_username,
                    password_hash,
                    name or clean_username,
                    department,
                    1 if is_admin else 0,
                ),
            )
        user = self.get_by_username(clean_username)
        if user is None:
            raise RuntimeError(f"Failed to create web user {clean_username}")
        return user

    def link_web_user(
        self,
        *,
        telegram_id: int,
        username: str,
        password: str,
        is_admin: bool = False,
    ) -> User:
        """Attach local web credentials to an existing Telegram-backed user."""
        user = self.get_by_telegram_id(telegram_id)
        if user is None:
            raise ValueError(f"telegram_id={telegram_id} is not registered")
        return self.link_web_login(
            user_id=user.id,
            username=username,
            password=password,
            is_admin=is_admin,
        )

    def link_web_login(
        self,
        *,
        user_id: int,
        username: str,
        password: str,
        is_admin: bool = False,
    ) -> User:
        """Attach local web credentials to an existing canonical user."""
        clean_username = self.normalize_username(username)
        self._validate_password(password)
        password_hash = self.hash_password(password)
        with db_connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if target is None:
                raise ValueError(f"user #{user_id} is not registered")

            existing = conn.execute(
                "SELECT id FROM users WHERE username = ? AND id != ?",
                (clean_username, int(target["id"])),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"username {clean_username!r} already belongs to another user")

            conn.execute(
                """
                UPDATE users
                SET username = ?, password_hash = ?, is_admin = MAX(is_admin, ?)
                WHERE id = ?
                """,
                (clean_username, password_hash, 1 if is_admin else 0, int(target["id"])),
            )

        user = self.get_by_id(user_id)
        if user is None:
            raise RuntimeError(f"Failed to link web login for user #{user_id}")
        return user

    def link_telegram_user(self, *, user_id: int, telegram_id: int) -> User:
        """Attach a Telegram identity to an existing canonical user."""
        with db_connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            target = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            if target is None:
                raise ValueError(f"user #{user_id} is not registered")
            existing = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ? AND id != ?",
                (telegram_id, user_id),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"telegram_id={telegram_id} already belongs to another user")
            conn.execute(
                "UPDATE users SET telegram_id = ? WHERE id = ?",
                (telegram_id, user_id),
            )

        user = self.get_by_id(user_id)
        if user is None:
            raise RuntimeError(f"Failed to link Telegram identity for user #{user_id}")
        return user

    def set_web_password(self, username: str, password: str) -> bool:
        """Set a local web user's password. Returns False if the user is missing."""
        clean_username = self.normalize_username(username)
        self._validate_password(password)
        password_hash = self.hash_password(password)
        with db_connect(self._db) as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (password_hash, clean_username),
            )
        return bool(cur.rowcount)

    def merge_web_user(
        self,
        *,
        source_user_id: int,
        target_user_id: int,
        workspace_base: Path | None = None,
        memory_db_path: Path | None = None,
    ) -> dict[str, int | str | bool]:
        """Merge a duplicate web-only user into the canonical user.

        The target user keeps its canonical Telegram identity. Web credentials are copied
        from source when target has no credentials, sessions are moved to target, source
        is disabled, and optional workspace/memory records are moved conservatively.
        """
        if source_user_id == target_user_id:
            raise ValueError("source and target users must be different")

        with db_connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            source = conn.execute("SELECT * FROM users WHERE id = ?", (source_user_id,)).fetchone()
            target = conn.execute("SELECT * FROM users WHERE id = ?", (target_user_id,)).fetchone()
            if source is None:
                raise ValueError(f"source user #{source_user_id} not found")
            if target is None:
                raise ValueError(f"target user #{target_user_id} not found")

            source_username = source["username"]
            target_username = target["username"]
            if source_username and target_username and str(source_username) != str(target_username):
                raise ValueError("target user already has a different web username")

            merged_username = target_username or source_username
            merged_password_hash = target["password_hash"] or source["password_hash"]
            merged_is_admin = 1 if bool(target["is_admin"]) or bool(source["is_admin"]) else 0

            conn.execute(
                "UPDATE web_sessions SET user_id = ? WHERE user_id = ?",
                (target_user_id, source_user_id),
            )
            conn.execute(
                """
                UPDATE users
                SET username = NULL, password_hash = NULL, disabled = 1
                WHERE id = ?
                """,
                (source_user_id,),
            )
            conn.execute(
                """
                UPDATE users
                SET username = ?, password_hash = ?, is_admin = ?
                WHERE id = ?
                """,
                (merged_username, merged_password_hash, merged_is_admin, target_user_id),
            )

        source_after = self.get_by_id(source_user_id)
        target_after = self.get_by_id(target_user_id)
        if source_after is None or target_after is None:
            raise RuntimeError("merge failed while reloading users")

        moved_workspace_items = 0
        if workspace_base is not None:
            moved_workspace_items = self._merge_workspace(
                workspace_base=workspace_base,
                source_key=source_after.workspace_key(),
                target_key=target_after.workspace_key(),
                source_user_id=source_user_id,
            )

        moved_messages = 0
        moved_facts = 0
        if memory_db_path is not None:
            moved_messages, moved_facts = self._merge_memory(
                memory_db_path=memory_db_path,
                source_key=source_after.memory_key(),
                target_key=target_after.memory_key(),
            )

        return {
            "source_user_id": source_user_id,
            "target_user_id": target_user_id,
            "target_workspace_key": target_after.workspace_key(),
            "moved_workspace_items": moved_workspace_items,
            "moved_messages": moved_messages,
            "moved_facts": moved_facts,
            "source_disabled": True,
        }

    def migrate_canonical_ids(
        self,
        *,
        workspace_base: Path | None = None,
        memory_db_path: Path | None = None,
        bootstrap_users_dir: Path | None = None,
    ) -> dict[str, int]:
        """Move legacy telegram_id-keyed user data to canonical DB id keys."""
        users = [user for user in self.list_users() if user.telegram_id is not None]
        moved_workspaces = 0
        moved_messages = 0
        moved_facts = 0
        moved_onboarding = 0
        moved_bootstrap = 0

        for user in users:
            assert user.telegram_id is not None
            legacy_key = str(user.telegram_id)
            canonical_key = str(user.id)
            if legacy_key == canonical_key:
                continue

            if workspace_base is not None:
                moved_workspaces += self._merge_workspace(
                    workspace_base=workspace_base,
                    source_key=legacy_key,
                    target_key=canonical_key,
                    source_user_id=user.telegram_id,
                )
            if memory_db_path is not None:
                messages, facts = self._merge_memory(
                    memory_db_path=memory_db_path,
                    source_key=legacy_key,
                    target_key=canonical_key,
                )
                moved_messages += messages
                moved_facts += facts
            moved_onboarding += self._migrate_onboarding_state(
                legacy_user_id=user.telegram_id,
                canonical_user_id=user.id,
            )
            if bootstrap_users_dir is not None:
                moved_bootstrap += self._migrate_bootstrap_file(
                    users_dir=bootstrap_users_dir,
                    legacy_key=legacy_key,
                    canonical_key=canonical_key,
                )

        return {
            "users": len(users),
            "workspace_items": moved_workspaces,
            "messages": moved_messages,
            "facts": moved_facts,
            "onboarding_states": moved_onboarding,
            "bootstrap_files": moved_bootstrap,
        }

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
            username=row["username"],
            is_admin=bool(row["is_admin"]),
            disabled=bool(row["disabled"]),
        )

    def get_by_id(self, user_id: int) -> User | None:
        """Look up a user by internal DB id."""
        with db_connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_username(self, username: str) -> User | None:
        """Look up a local web user by username."""
        clean_username = username.strip().lower()
        with db_connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (clean_username,)
            ).fetchone()
        return self._row_to_user(row) if row else None

    def authenticate_web_user(self, username: str, password: str) -> User | None:
        """Validate local web credentials and return the user if active."""
        clean_username = username.strip().lower()
        with db_connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (clean_username,)
            ).fetchone()
        if not row or bool(row["disabled"]) or not row["password_hash"]:
            return None
        if not self.verify_password(password, str(row["password_hash"])):
            return None
        return self._row_to_user(row)

    def create_web_session(self, user_id: int, ttl_hours: int = 12) -> tuple[str, str]:
        """Create a web session and return (raw_token, csrf_token)."""
        raw_token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
        token_hash = self._hash_session_token(raw_token)
        csrf_token = secrets.token_urlsafe(24)
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
        with db_connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO web_sessions (token_hash, user_id, csrf_token, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (token_hash, user_id, csrf_token, expires_at.isoformat()),
            )
        return raw_token, csrf_token

    def get_user_by_session(self, raw_token: str) -> tuple[User, str] | None:
        """Return (user, csrf_token) for a valid unexpired web session."""
        token_hash = self._hash_session_token(raw_token)
        with db_connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT s.csrf_token, s.expires_at, u.*
                FROM web_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        if not row:
            return None
        try:
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
        except ValueError:
            return None
        if expires_at <= datetime.now(UTC) or bool(row["disabled"]):
            self.delete_web_session(raw_token)
            return None
        return self._row_to_user(row), str(row["csrf_token"])

    def delete_web_session(self, raw_token: str) -> None:
        """Delete a web session by raw token."""
        token_hash = self._hash_session_token(raw_token)
        with db_connect(self._db) as conn:
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    def prune_expired_web_sessions(self) -> int:
        """Delete expired web sessions and return removed count."""
        with db_connect(self._db) as conn:
            cur = conn.execute(
                "DELETE FROM web_sessions WHERE expires_at <= ?",
                (datetime.now(UTC).isoformat(),),
            )
        return int(cur.rowcount or 0)

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
                username=r["username"],
                is_admin=bool(r["is_admin"]),
                disabled=bool(r["disabled"]),
            )
            for r in rows
        ]

    @staticmethod
    def _hash_session_token(raw_token: str) -> str:
        return pbkdf2_hmac(
            "sha256",
            raw_token.encode("utf-8"),
            b"corpclaw-lite-web-session",
            1,
        ).hex()

    def _validate_password(self, password: str) -> None:
        if not password:
            raise ValueError("password is required")
        if len(password) < self._password_min_length:
            raise ValueError(f"password must be at least {self._password_min_length} characters")
        if len(password) > self._password_max_length:
            raise ValueError(f"password must be at most {self._password_max_length} characters")

    @staticmethod
    def _merge_workspace(
        *,
        workspace_base: Path,
        source_key: str,
        target_key: str,
        source_user_id: int,
    ) -> int:
        source_dir = (Path(workspace_base) / f"user_{source_key}").resolve()
        target_dir = (Path(workspace_base) / f"user_{target_key}").resolve()
        if source_dir == target_dir or not source_dir.exists():
            return 0
        target_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for item in source_dir.iterdir():
            destination = target_dir / item.name
            if destination.exists():
                destination = UserManager._conflict_path(
                    target_dir / f"{item.name}.from_user_{source_user_id}"
                )
            shutil.move(str(item), str(destination))
            moved += 1
        try:
            source_dir.rmdir()
        except OSError:
            logger.warning(
                "Merged workspace %s but could not remove non-empty directory",
                source_dir,
            )
        return moved

    @staticmethod
    def _conflict_path(path: Path) -> Path:
        if not path.exists():
            return path
        for i in range(2, 10_000):
            candidate = path.with_name(f"{path.name}.{i}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Cannot find a free conflict path for {path}")

    @staticmethod
    def _merge_memory(*, memory_db_path: Path, source_key: str, target_key: str) -> tuple[int, int]:
        if source_key == target_key or not memory_db_path.exists():
            return 0, 0
        with db_connect(memory_db_path) as conn:
            cur = conn.execute(
                "UPDATE messages SET user_id = ? WHERE user_id = ?",
                (target_key, source_key),
            )
            moved_messages = int(cur.rowcount or 0)
            cur = conn.execute(
                """
                UPDATE memory_facts
                SET user_id = ?
                WHERE user_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_facts existing
                      WHERE existing.user_id = ?
                        AND existing.key = memory_facts.key
                  )
                """,
                (target_key, source_key, target_key),
            )
            moved_facts = int(cur.rowcount or 0)
            conn.execute("DELETE FROM memory_facts WHERE user_id = ?", (source_key,))
            try:
                target_active = conn.execute(
                    """
                    SELECT 1 FROM web_chat_sessions
                    WHERE user_id = ? AND ended_at IS NULL
                    LIMIT 1
                    """,
                    (target_key,),
                ).fetchone()
                if target_active is not None:
                    conn.execute(
                        """
                        UPDATE web_chat_sessions
                        SET ended_at = CURRENT_TIMESTAMP,
                            reset_reason = ?
                        WHERE user_id = ? AND ended_at IS NULL
                        """,
                        (f"merged_into_user_{target_key}", source_key),
                    )
                conn.execute(
                    "UPDATE web_chat_sessions SET user_id = ? WHERE user_id = ?",
                    (target_key, source_key),
                )
                conn.execute(
                    "UPDATE web_chat_messages SET user_id = ? WHERE user_id = ?",
                    (target_key, source_key),
                )
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e).lower():
                    raise
        return moved_messages, moved_facts

    def _migrate_onboarding_state(self, *, legacy_user_id: int, canonical_user_id: int) -> int:
        if legacy_user_id == canonical_user_id:
            return 0
        with db_connect(self._db) as conn:
            try:
                legacy = conn.execute(
                    "SELECT 1 FROM onboarding_state WHERE user_id = ?",
                    (legacy_user_id,),
                ).fetchone()
            except sqlite3.OperationalError as e:
                if "no such table" in str(e).lower():
                    return 0
                raise
            if legacy is None:
                return 0
            canonical = conn.execute(
                "SELECT 1 FROM onboarding_state WHERE user_id = ?",
                (canonical_user_id,),
            ).fetchone()
            if canonical is None:
                conn.execute(
                    "UPDATE onboarding_state SET user_id = ? WHERE user_id = ?",
                    (canonical_user_id, legacy_user_id),
                )
            else:
                conn.execute(
                    "DELETE FROM onboarding_state WHERE user_id = ?",
                    (legacy_user_id,),
                )
        return 1

    @staticmethod
    def _migrate_bootstrap_file(*, users_dir: Path, legacy_key: str, canonical_key: str) -> int:
        if legacy_key == canonical_key:
            return 0
        legacy_path = Path(users_dir) / f"{legacy_key}.md"
        if not legacy_path.exists():
            return 0
        canonical_path = Path(users_dir) / f"{canonical_key}.md"
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        destination = canonical_path
        if destination.exists():
            destination = UserManager._conflict_path(
                canonical_path.with_name(f"{canonical_path.stem}.from_telegram_{legacy_key}.md")
            )
        shutil.move(str(legacy_path), str(destination))
        return 1

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            name=row["name"],
            department=row["department"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            is_admin=bool(row["is_admin"]),
            disabled=bool(row["disabled"]),
        )

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
        from corpclaw_lite.utils.fs import atomic_write_text

        atomic_write_text(self._whitelist_path, json.dumps(entries, indent=2), encoding="utf-8")
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
        from corpclaw_lite.utils.fs import atomic_write_text

        atomic_write_text(self._revoked_path, json.dumps(sorted(revoked)), encoding="utf-8")
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

    async def async_get_by_id(self, user_id: int) -> User | None:
        """Async wrapper around get_by_id."""
        return await anyio.to_thread.run_sync(partial(self.get_by_id, user_id))

    async def async_create_user(self, telegram_id: int, department: str, name: str = "") -> User:
        """Async wrapper around create_user (runs SQLite in thread)."""
        return await anyio.to_thread.run_sync(
            partial(self.create_user, telegram_id=telegram_id, department=department, name=name)
        )

    def update_name(self, user_id: int, name: str) -> None:
        """Update user display name (e.g. after onboarding)."""
        with db_connect(self._db) as conn:
            conn.execute(
                "UPDATE users SET name = ? WHERE id = ?",
                (name, user_id),
            )
        logger.info("Updated name for user_id=%d: %s", user_id, name)

    async def async_update_name(self, user_id: int, name: str) -> None:
        """Async wrapper around update_name."""
        await anyio.to_thread.run_sync(partial(self.update_name, user_id, name))

    def get_agent_context(self, user_id: int) -> dict[str, str] | None:
        """Return the user's agent context (instructions + tone), or None if unset."""
        try:
            with db_connect(self._db) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT instructions, tone FROM user_agent_context WHERE user_id = ?",
                    (int(user_id),),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "instructions": str(row["instructions"]),  # type: ignore[index]
                    "tone": str(row["tone"]),  # type: ignore[index]
                }
        except Exception as e:
            logger.warning("Failed to get agent context for user %s: %s", user_id, e)
            return None

    async def async_get_agent_context(self, user_id: int) -> dict[str, str] | None:
        """Async wrapper around get_agent_context."""
        return await anyio.to_thread.run_sync(partial(self.get_agent_context, user_id))

    def set_agent_context(self, user_id: int, *, instructions: str, tone: str) -> None:
        """Upsert the user's agent context (personal instructions + tone)."""
        if tone not in ("default", "concise", "detailed"):
            tone = "default"
        instructions = instructions.strip()[:10000]  # cap at 10k chars
        try:
            with db_connect(self._db) as conn:
                conn.execute(
                    """
                    INSERT INTO user_agent_context (user_id, instructions, tone, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        instructions = excluded.instructions,
                        tone = excluded.tone,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (int(user_id), instructions, tone),
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to set agent context for user %s: %s", user_id, e)

    async def async_set_agent_context(self, user_id: int, *, instructions: str, tone: str) -> None:
        """Async wrapper around set_agent_context."""
        await anyio.to_thread.run_sync(
            partial(self.set_agent_context, user_id, instructions=instructions, tone=tone)
        )
