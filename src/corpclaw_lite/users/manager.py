# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import json
import logging
import secrets
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import pbkdf2_hmac
from pathlib import Path
from typing import Any, cast

import anyio

from corpclaw_lite.users.models import User
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "MemoryWorkerState",
    "UserManager",
    "tone_directive",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryWorkerState:
    """B-109: per-user memory-worker opt-in state + last-run metadata."""

    user_id: int
    enabled: bool
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None


# Response-tone directives injected into the system prompt per the user's
# ``user_agent_context.tone`` setting (Etap 5). ``"default"`` has no directive —
# the base SOUL.md tone line applies unchanged. ``tone_directive()`` is the
# single point consumed by ``AgentLoop`` (B-111) for both run-time assembly and
# web preview, so the tone setting can never silently go unused.
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
    Manages user storage in SQLite (default path: data/users.db).

    Cross-chat agent facts live in SQLiteMemory (memory.db / memory_entries), not here.
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
        self._whitelist_path = self._db.parent / "whitelist.json"
        self._revoked_path = self._db.parent / "revoked_sessions.json"
        self._password_min_length = max(1, password_min_length)
        self._password_max_length = max(self._password_min_length, password_max_length)
        self._init_db()
        self._migrate_legacy_auth_json()

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_whitelist (
                    telegram_id INTEGER PRIMARY KEY,
                    department TEXT NOT NULL DEFAULT 'default',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_revocations (
                    telegram_id INTEGER PRIMARY KEY,
                    revoked_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
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
            # B-109: per-user memory worker opt-in + run history.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memory_worker (
                    user_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    last_run_at TEXT,
                    last_status TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

    @staticmethod
    def _read_legacy_whitelist(path: Path) -> list[tuple[int, str]]:
        if not path.exists():
            return []
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Cannot migrate legacy whitelist {path}: {exc}") from exc
        if not isinstance(raw, list):
            raise RuntimeError(f"Cannot migrate legacy whitelist {path}: expected a JSON list")
        result: list[tuple[int, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise RuntimeError(f"Cannot migrate legacy whitelist {path}: invalid entry")
            telegram_id = entry.get("telegram_id")
            department = entry.get("department", "default")
            if isinstance(telegram_id, bool) or not isinstance(telegram_id, int):
                raise RuntimeError(f"Cannot migrate legacy whitelist {path}: invalid telegram_id")
            if not isinstance(department, str) or not department:
                raise RuntimeError(f"Cannot migrate legacy whitelist {path}: invalid department")
            result.append((telegram_id, department))
        return result

    @staticmethod
    def _read_legacy_revocations(path: Path) -> list[int]:
        if not path.exists():
            return []
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Cannot migrate legacy revocations {path}: {exc}") from exc
        if not isinstance(raw, list):
            raise RuntimeError(f"Cannot migrate legacy revocations {path}: expected a JSON list")
        result: list[int] = []
        for item in raw:
            if isinstance(item, bool) or not isinstance(item, (int, str)):
                raise RuntimeError(f"Cannot migrate legacy revocations {path}: invalid id")
            try:
                result.append(int(item))
            except ValueError as exc:
                raise RuntimeError(f"Cannot migrate legacy revocations {path}: invalid id") from exc
        return result

    def _migrate_legacy_auth_json(self) -> None:
        """Import the pre-Sprint-1 JSON stores once; SQLite is canonical afterwards."""
        with db_connect(self._db) as conn:
            marker = conn.execute(
                "SELECT 1 FROM app_metadata WHERE key = 'auth_json_import_v1'"
            ).fetchone()
        if marker is not None:
            return

        whitelist = self._read_legacy_whitelist(self._whitelist_path)
        revoked = self._read_legacy_revocations(self._revoked_path)
        with db_connect(self._db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            marker = conn.execute(
                "SELECT 1 FROM app_metadata WHERE key = 'auth_json_import_v1'"
            ).fetchone()
            if marker is not None:
                return
            conn.executemany(
                "INSERT OR IGNORE INTO telegram_whitelist (telegram_id, department) VALUES (?, ?)",
                whitelist,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO telegram_revocations (telegram_id) VALUES (?)",
                [(telegram_id,) for telegram_id in revoked],
            )
            conn.execute(
                "INSERT INTO app_metadata (key, value) VALUES ('auth_json_import_v1', 'done')"
            )
        if whitelist or revoked:
            logger.info(
                "Imported legacy auth JSON into SQLite (whitelist=%d, revoked=%d)",
                len(whitelist),
                len(revoked),
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
        # S2-07: a password without a username was previously silently dropped.
        # Make the contract explicit — web login requires a username, so a
        # password without one is a caller error.
        if password is not None and clean_username is None:
            raise ValueError("Cannot set a password without a username")
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
        """Set a local web user's password. Returns False if the user is missing.

        S3-08: all existing web sessions for the user are invalidated atomically,
        so a session established before a password rotation (e.g. after suspected
        compromise) cannot remain valid until its TTL.
        """
        clean_username = self.normalize_username(username)
        self._validate_password(password)
        password_hash = self.hash_password(password)
        with db_connect(self._db) as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE username = ?", (clean_username,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (password_hash, clean_username),
            )
            conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (int(row[0]),))
        return True

    def merge_web_user(
        self,
        *,
        source_user_id: int,
        target_user_id: int,
        workspace_base: Path | None = None,
        memory_db_path: Path | None = None,
        feedback_db_path: Path | None = None,
        scheduler_db_path: Path | None = None,
        bootstrap_users_dir: Path | None = None,
    ) -> dict[str, int | str | bool]:
        """Merge a duplicate web-only user into the canonical user.

        The target user keeps its canonical Telegram identity. Web credentials are copied
        from source when target has no credentials, sessions are moved to target, source
        is disabled, and optional workspace/memory records are moved conservatively.

        H-4 (code review): also reparents feedback_labels, scheduled_tasks, and
        onboarding state/bootstrap file so the merge does not silently orphan
        user-keyed data outside memory.db.
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
            # B-074/M4: copy credentials onto the target first (needed to resolve
            # target.workspace_key() below) and null the source's credentials so
            # the source can no longer log in, but do NOT set disabled=1 yet.
            # The disabled flag is set only after the workspace/memory
            # sub-migrations succeed, so a mid-merge failure leaves the source
            # recoverable (credentials already moved, but the merge can be
            # retried or rolled back rather than leaving a half-moved disabled
            # user). Nulling source.username here also clears the UNIQUE
            # constraint so the target can take it over.
            conn.execute(
                """
                UPDATE users
                SET username = NULL, password_hash = NULL
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

        # H-4 (code review): reparent user-keyed data in the other DBs and the
        # onboarding/bootstrap artefacts so the merge does not orphan them.
        # All best-effort: a missing DB (lazy creation) or table is a no-op.
        moved_feedback = 0
        if feedback_db_path is not None:
            moved_feedback = self._reparent_feedback(
                feedback_db_path=feedback_db_path,
                source_key=source_after.memory_key(),
                target_key=target_after.memory_key(),
            )
        moved_scheduler = 0
        if scheduler_db_path is not None:
            moved_scheduler = self._reparent_scheduler(
                scheduler_db_path=scheduler_db_path,
                source_key=str(source_user_id),
                target_key=str(target_user_id),
            )
        moved_onboarding = self._migrate_onboarding_state(
            legacy_user_id=source_user_id,
            canonical_user_id=target_user_id,
        )
        moved_bootstrap = 0
        if bootstrap_users_dir is not None:
            moved_bootstrap = self._migrate_bootstrap_file(
                users_dir=bootstrap_users_dir,
                legacy_key=source_after.memory_key(),
                canonical_key=target_after.memory_key(),
            )

        # B-074/M4: disable the source only after all sub-migrations succeeded.
        # A failure above propagates with the source still enabled and its data
        # intact (recoverable), rather than disabled with half-moved memory.
        with db_connect(self._db) as conn:
            conn.execute(
                """
                UPDATE users
                SET username = NULL, password_hash = NULL, disabled = 1
                WHERE id = ?
                """,
                (source_user_id,),
            )

        return {
            "source_user_id": source_user_id,
            "target_user_id": target_user_id,
            "target_workspace_key": target_after.workspace_key(),
            "moved_workspace_items": moved_workspace_items,
            "moved_messages": moved_messages,
            "moved_facts": moved_facts,
            "moved_feedback_labels": moved_feedback,
            "moved_scheduler_tasks": moved_scheduler,
            "moved_onboarding_states": moved_onboarding,
            "moved_bootstrap_files": moved_bootstrap,
            "source_disabled": True,
        }

    def migrate_canonical_ids(
        self,
        *,
        workspace_base: Path | None = None,
        memory_db_path: Path | None = None,
        bootstrap_users_dir: Path | None = None,
        feedback_db_path: Path | None = None,
        scheduler_db_path: Path | None = None,
    ) -> dict[str, int]:
        """Move legacy telegram_id-keyed user data to canonical DB id keys.

        H-4 (code review): also reparents feedback_labels and scheduled_tasks
        (in their own DBs) so the migration does not silently orphan them.
        """
        users = [user for user in self.list_users() if user.telegram_id is not None]
        moved_workspaces = 0
        moved_messages = 0
        moved_facts = 0
        moved_onboarding = 0
        moved_bootstrap = 0
        moved_feedback = 0
        moved_scheduler = 0

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
            # H-4: feedback.db user_id is TEXT (memory-key-shaped).
            if feedback_db_path is not None:
                moved_feedback += self._reparent_feedback(
                    feedback_db_path=feedback_db_path,
                    source_key=legacy_key,
                    target_key=canonical_key,
                )
            # H-4: scheduler.db user_id is INTEGER (telegram_id-shaped).
            if scheduler_db_path is not None:
                moved_scheduler += self._reparent_scheduler(
                    scheduler_db_path=scheduler_db_path,
                    source_key=legacy_key,
                    target_key=canonical_key,
                )

        return {
            "users": len(users),
            "workspace_items": moved_workspaces,
            "messages": moved_messages,
            "facts": moved_facts,
            "onboarding_states": moved_onboarding,
            "bootstrap_files": moved_bootstrap,
            "feedback_labels": moved_feedback,
            "scheduler_tasks": moved_scheduler,
        }

    def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Look up a user by their Telegram ID."""
        with db_connect(self._db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return self._row_to_user(row) if row else None

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
        return [self._row_to_user(r) for r in rows]

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
        """Move memory facts (and optional legacy tables) from source to target user.

        D-078 / B-106: ``SQLiteMemory`` is facts-only and drops the legacy
        ``messages`` table on init. The legacy ``UPDATE messages`` is best-effort
        only (no-op when the table is absent) so merge does not fail on modern DBs.
        """
        if source_key == target_key or not memory_db_path.exists():
            return 0, 0
        with db_connect(memory_db_path) as conn:
            # Legacy transcript table (pre-D-078). Optional — often missing.
            moved_messages = 0
            try:
                cur = conn.execute(
                    "UPDATE messages SET user_id = ? WHERE user_id = ?",
                    (target_key, source_key),
                )
                moved_messages = int(cur.rowcount or 0)
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e).lower():
                    raise

            # B-108: memory_entries (abstraction UNIQUE per user). Legacy memory_facts optional.
            moved_facts = 0
            try:
                cur = conn.execute(
                    """
                    UPDATE memory_entries
                    SET user_id = ?
                    WHERE user_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM memory_entries existing
                          WHERE existing.user_id = ?
                            AND existing.primary_abstraction =
                                memory_entries.primary_abstraction
                      )
                    """,
                    (target_key, source_key, target_key),
                )
                moved_facts = int(cur.rowcount or 0)
                conn.execute("DELETE FROM memory_entries WHERE user_id = ?", (source_key,))
                # Best-effort FTS cleanup (table may be missing).
                try:
                    conn.execute(
                        "DELETE FROM memory_entries_fts WHERE user_id = ?",
                        (source_key,),
                    )
                    # Re-index moved rows for target (application dual-write normally
                    # keeps FTS in sync; after raw SQL merge rebuild rows for target).
                    rows = conn.execute(
                        """
                        SELECT id, user_id, primary_abstraction, cue_indices_json
                        FROM memory_entries WHERE user_id = ?
                        """,
                        (target_key,),
                    ).fetchall()
                    for row in rows:
                        rid, uid, abstr, cues_json = row[0], row[1], row[2], row[3]
                        try:
                            parsed_cues: Any = json.loads(cues_json) if cues_json else []
                            if isinstance(parsed_cues, list):
                                cues_text = " ".join(
                                    str(item) for item in cast(list[Any], parsed_cues)
                                )
                            else:
                                cues_text = ""
                        except Exception:
                            cues_text = ""
                        conn.execute(
                            "DELETE FROM memory_entries_fts WHERE rowid = ?",
                            (rid,),
                        )
                        conn.execute(
                            """
                            INSERT INTO memory_entries_fts(
                                rowid, user_id, primary_abstraction, cues
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (rid, uid, abstr, cues_text),
                        )
                except sqlite3.OperationalError:
                    pass
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e).lower():
                    raise
                # Pre-B-108 DBs
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
                # H-4 (code review): the user-keyed tables below were silently
                # orphaned on merge — sessions moved to target but their child
                # rows stayed on source, and list_context / list_pins filter by
                # user_id, so target never saw them. Reparent each in turn; a
                # missing table on an older DB is a no-op (skip, not raise).
                for table in (
                    "web_chat_context",
                    "web_chat_pins",
                    "agent_change_sets",
                    "agent_file_changes",
                ):
                    try:
                        conn.execute(
                            f"UPDATE {table} SET user_id = ? WHERE user_id = ?",
                            (target_key, source_key),
                        )
                    except sqlite3.OperationalError as e:
                        if "no such table" not in str(e).lower():
                            raise
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e).lower():
                    raise
        return moved_messages, moved_facts

    @staticmethod
    def _reparent_feedback(*, feedback_db_path: Path, source_key: str, target_key: str) -> int:
        """Move feedback_labels rows for source user to target (H-4, code review).

        Lives in a separate DB (data/feedback.db), so not covered by _merge_memory.
        Best-effort: a missing DB (lazy creation) or table is a no-op.
        """
        if source_key == target_key or not feedback_db_path.exists():
            return 0
        try:
            with db_connect(feedback_db_path) as conn:
                cur = conn.execute(
                    "UPDATE feedback_labels SET user_id = ? WHERE user_id = ?",
                    (target_key, source_key),
                )
                return int(cur.rowcount or 0)
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            return 0

    @staticmethod
    def _reparent_scheduler(*, scheduler_db_path: Path, source_key: str, target_key: str) -> int:
        """Move scheduled_tasks rows for source user to target (H-4, code review).

        Lives in a separate DB (data/scheduler.db), so not covered by _merge_memory.
        Best-effort: a missing DB (lazy creation) or table is a no-op. ``run_log``
        rows are FK-CASCADEd to their task, so they follow automatically.
        """
        if source_key == target_key or not scheduler_db_path.exists():
            return 0
        try:
            with db_connect(scheduler_db_path) as conn:
                cur = conn.execute(
                    "UPDATE scheduled_tasks SET user_id = ? WHERE user_id = ?",
                    (target_key, source_key),
                )
                return int(cur.rowcount or 0)
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            return 0

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
        # S2-06: read created_at from the DB row (previously dropped everywhere).
        created_raw = row["created_at"] if "created_at" in row.keys() else None  # noqa: SIM118
        if isinstance(created_raw, datetime):
            created_at = created_raw
        else:
            try:
                created_at = datetime.fromisoformat(str(created_raw))
            except (ValueError, TypeError):
                created_at = datetime.now(UTC)
        return User(
            id=row["id"],
            name=row["name"],
            department=row["department"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            is_admin=bool(row["is_admin"]),
            disabled=bool(row["disabled"]),
            created_at=created_at,
        )

    def _get_id_by_telegram(self, telegram_id: int) -> int:
        with db_connect(self._db) as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    # ── Whitelist ─────────────────────────────────────────────────────────────

    def _load_whitelist(self) -> list[dict[str, int | str]]:
        """Load the canonical whitelist directly from SQLite."""
        with db_connect(self._db) as conn:
            rows = conn.execute(
                "SELECT telegram_id, department FROM telegram_whitelist ORDER BY telegram_id"
            ).fetchall()
        return [
            {"telegram_id": int(telegram_id), "department": str(department)}
            for telegram_id, department in rows
        ]

    def seed_whitelist(self, telegram_ids: list[int], default_department: str) -> None:
        """Merge config-based whitelist IDs into the persistent file.

        Only adds IDs that are not already present. Called once at startup.
        """
        with db_connect(self._db) as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO telegram_whitelist (telegram_id, department) VALUES (?, ?)",
                [(tid, default_department) for tid in telegram_ids],
            )
            added = conn.total_changes - before
        if added:
            logger.info("Seeded %d IDs into whitelist", added)

    def add_to_whitelist(self, telegram_id: int, department: str = "default") -> None:
        """Add a telegram_id to the persistent whitelist."""
        with db_connect(self._db) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO telegram_whitelist (telegram_id, department) VALUES (?, ?)",
                (telegram_id, department),
            )
        if not cur.rowcount:
            logger.info("telegram_id=%d already in whitelist", telegram_id)
            return
        logger.info("Added telegram_id=%d to whitelist (dept=%s)", telegram_id, department)

    def remove_from_whitelist(self, telegram_id: int) -> bool:
        """Remove a telegram_id from the persistent whitelist. Returns True if found."""
        with db_connect(self._db) as conn:
            cur = conn.execute(
                "DELETE FROM telegram_whitelist WHERE telegram_id = ?", (telegram_id,)
            )
        if not cur.rowcount:
            return False
        logger.info("Removed telegram_id=%d from whitelist", telegram_id)
        return True

    def get_whitelist(self) -> list[dict[str, int | str]]:
        """Return the full whitelist."""
        return self._load_whitelist()

    def is_allowed(self, telegram_id: int) -> bool:
        """Check if telegram_id is in the whitelist (deny-by-default)."""
        with db_connect(self._db) as conn:
            row = conn.execute(
                "SELECT 1 FROM telegram_whitelist WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return row is not None

    def get_whitelist_department(self, telegram_id: int) -> str:
        """Return department for a whitelisted telegram_id, or 'default'."""
        with db_connect(self._db) as conn:
            row = conn.execute(
                "SELECT department FROM telegram_whitelist WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return str(row[0]) if row is not None else "default"

    # ── Revoked Sessions ──────────────────────────────────────────────────────

    def _load_revoked(self) -> set[int]:
        with db_connect(self._db) as conn:
            rows = conn.execute("SELECT telegram_id FROM telegram_revocations").fetchall()
        return {int(row[0]) for row in rows}

    def revoke_session(self, telegram_id: int) -> None:
        """Block a user from interacting with the bot."""
        with db_connect(self._db) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO telegram_revocations (telegram_id) VALUES (?)",
                (telegram_id,),
            )
        logger.info("Session revoked for telegram_id=%d", telegram_id)

    def unrevoke_session(self, telegram_id: int) -> None:
        """Unblock a previously revoked user."""
        with db_connect(self._db) as conn:
            conn.execute("DELETE FROM telegram_revocations WHERE telegram_id = ?", (telegram_id,))

    def is_session_revoked(self, telegram_id: int) -> bool:
        """Check if a user's session is revoked."""
        with db_connect(self._db) as conn:
            row = conn.execute(
                "SELECT 1 FROM telegram_revocations WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return row is not None

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
        """Upsert the user's agent context (personal instructions + tone).

        S1-11: DB write errors now propagate (callers handle them) instead of
        being swallowed — a silently-lost instruction/tone update is a data-loss
        bug with no signal to the API layer.
        """
        if tone not in ("default", "concise", "detailed"):
            tone = "default"
        instructions = instructions.strip()[:10000]  # cap at 10k chars
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

    async def async_set_agent_context(self, user_id: int, *, instructions: str, tone: str) -> None:
        """Async wrapper around set_agent_context."""
        await anyio.to_thread.run_sync(
            partial(self.set_agent_context, user_id, instructions=instructions, tone=tone)
        )

    # ── B-109: Memory worker opt-in + run history ───────────────────────────

    def set_memory_worker_enabled(self, user_id: int, enabled: bool) -> None:
        """Opt a user in/out of the background memory worker (B-109).

        S1-11: DB write errors now propagate (callers handle them) instead of
        being swallowed.
        """
        now = datetime.now(UTC).isoformat()
        with db_connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO user_memory_worker (user_id, enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), 1 if enabled else 0, now),
            )

    async def async_set_memory_worker_enabled(self, user_id: int, enabled: bool) -> None:
        """Async wrapper around set_memory_worker_enabled."""
        await anyio.to_thread.run_sync(partial(self.set_memory_worker_enabled, user_id, enabled))

    def get_memory_worker_state(self, user_id: int) -> MemoryWorkerState | None:
        """Return the user's memory-worker state, or None if no row exists."""
        try:
            with db_connect(self._db) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM user_memory_worker WHERE user_id = ?",
                    (int(user_id),),
                ).fetchone()
        except Exception as e:
            logger.warning("Failed to get memory_worker state for user %s: %s", user_id, e)
            return None
        if row is None:
            return None
        return MemoryWorkerState(
            user_id=int(row["user_id"]),
            enabled=bool(row["enabled"]),
            last_run_at=str(row["last_run_at"]) if row["last_run_at"] is not None else None,
            last_status=str(row["last_status"]) if row["last_status"] is not None else None,
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        )

    async def async_get_memory_worker_state(self, user_id: int) -> MemoryWorkerState | None:
        """Async wrapper around get_memory_worker_state."""
        return await anyio.to_thread.run_sync(partial(self.get_memory_worker_state, user_id))

    def list_memory_worker_enabled_users(self) -> list[int]:
        """Return user_ids that have opted in to the memory worker."""
        try:
            with db_connect(self._db) as conn:
                rows = conn.execute(
                    "SELECT user_id FROM user_memory_worker WHERE enabled = 1"
                ).fetchall()
        except Exception as e:
            logger.warning("Failed to list memory_worker enabled users: %s", e)
            return []
        return [int(r[0]) for r in rows]

    async def async_list_memory_worker_enabled_users(self) -> list[int]:
        """Async wrapper around list_memory_worker_enabled_users."""
        return await anyio.to_thread.run_sync(self.list_memory_worker_enabled_users)

    def update_memory_worker_run(
        self,
        user_id: int,
        *,
        status: str,
        error: str | None = None,
        advance_last_run_at: bool = False,
    ) -> None:
        """Record the outcome of a memory-worker run for the user.

        Does NOT auto-enable: if no row exists, inserts with ``enabled=0`` so the
        opt-in guarantee is preserved (worker should only call this for already
        opted-in users, but defense-in-depth).

        S1-11: ``advance_last_run_at`` controls whether ``last_run_at`` is bumped
        to now. Only a genuinely completed run (``status="ok"``) should advance
        it — skip/error outcomes must NOT demote the user in the
        ``_sort_users_by_last_run`` ordering, otherwise a consistently-busy user
        is starved (its skip sets a fresh timestamp every tick). status/error are
        always recorded for observability.
        """
        now = datetime.now(UTC).isoformat()
        truncated_error = error[:2000] if error else None
        with db_connect(self._db) as conn:
            if advance_last_run_at:
                conn.execute(
                    """
                    INSERT INTO user_memory_worker (
                        user_id, enabled, last_run_at, last_status, last_error, updated_at
                    )
                    VALUES (?, 0, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        last_run_at = excluded.last_run_at,
                        last_status = excluded.last_status,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (int(user_id), now, status, truncated_error, now),
                )
            else:
                # Record status/error but leave last_run_at untouched so the user
                # is not demoted in the run-ordering sort.
                conn.execute(
                    """
                    INSERT INTO user_memory_worker (
                        user_id, enabled, last_status, last_error, updated_at
                    )
                    VALUES (?, 0, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        last_status = excluded.last_status,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (int(user_id), status, truncated_error, now),
                )

    async def async_update_memory_worker_run(
        self,
        user_id: int,
        *,
        status: str,
        error: str | None = None,
        advance_last_run_at: bool = False,
    ) -> None:
        """Async wrapper around update_memory_worker_run."""
        await anyio.to_thread.run_sync(
            partial(
                self.update_memory_worker_run,
                user_id,
                status=status,
                error=error,
                advance_last_run_at=advance_last_run_at,
            )
        )
