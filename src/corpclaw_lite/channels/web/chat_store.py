from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from corpclaw_lite.exceptions import StorageError
from corpclaw_lite.paths import DATA_DIR
from corpclaw_lite.utils.async_helpers import run_in_thread
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "ChatSessionSummary",
    "WebChatFile",
    "WebChatMessage",
    "WebChatPage",
    "WebChatStore",
]

logger = logging.getLogger(__name__)

_MAX_PERSISTED_CONTENT_CHARS = 200_000
_DEFAULT_HISTORY_LIMIT = 100
_DEFAULT_ACTIVE_MAX_MESSAGES = 2000


def _empty_metadata() -> dict[str, Any]:
    return {}


@dataclass(slots=True)
class WebChatFile:
    """File attachment metadata persisted for user-visible web chat cards."""

    name: str
    path: str | None = None
    caption: str = ""


@dataclass(slots=True)
class WebChatMessage:
    """A message from the user-visible web transcript."""

    id: int
    session_id: int
    user_id: str
    role: str
    content: str
    tone: str | None = None
    request_id: str | None = None
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    file: WebChatFile | None = None


@dataclass(slots=True)
class WebChatPage:
    """A page of transcript messages for one active web chat session."""

    session_id: int
    messages: list[WebChatMessage]
    has_more: bool


@dataclass(slots=True)
class ChatSessionSummary:
    """A chat session as shown in the sidebar chat list."""

    id: int
    user_id: str
    section: str
    title: str | None
    created_at: str
    is_active: bool
    msg_count: int


class WebChatStore:
    """Persistent user-visible transcript storage for the web channel.

    This store intentionally lives next to, but separately from, agent memory.
    Agent memory can be compacted/consolidated for model context quality, while
    this transcript remains a stable UI history for refresh/reconnect/login.
    """

    def __init__(
        self,
        db_path: str | Path = DATA_DIR / "memory.db",
        *,
        active_max_messages: int = _DEFAULT_ACTIVE_MAX_MESSAGES,
    ) -> None:
        self.db_path = Path(db_path)
        self._active_max_messages = max(1, active_max_messages)
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS web_chat_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        ended_at DATETIME,
                        reset_reason TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_web_chat_sessions_active
                    ON web_chat_sessions(user_id)
                    WHERE ended_at IS NULL
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_web_chat_sessions_user
                    ON web_chat_sessions(user_id, id)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS web_chat_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL,
                        user_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        tone TEXT,
                        request_id TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        file_name TEXT,
                        file_path TEXT,
                        file_caption TEXT,
                        FOREIGN KEY(session_id) REFERENCES web_chat_sessions(id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_web_chat_messages_session
                    ON web_chat_messages(session_id, id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_web_chat_messages_user
                    ON web_chat_messages(user_id, id)
                    """
                )
                # Etap 2: add section/title columns to sessions (idempotent).
                # Mirrors the reasoning-column migration in memory/sqlite.py:
                # ALTER on every init; "duplicate column name" is swallowed.
                # `section DEFAULT 'chat'` backfills legacy sessions into the Chat section.
                for col, decl in [
                    ("section", "TEXT NOT NULL DEFAULT 'chat'"),
                    ("title", "TEXT"),
                ]:
                    with contextlib.suppress(sqlite3.OperationalError):
                        # "duplicate column name" when already migrated — swallow.
                        conn.execute(f"ALTER TABLE web_chat_sessions ADD COLUMN {col} {decl}")
        except Exception as e:
            logger.critical("Failed to initialize web chat store: %s", e)
            raise StorageError(f"Web chat store initialization failed: {e}") from e

    @staticmethod
    def _clean_content(content: str) -> str:
        if len(content) <= _MAX_PERSISTED_CONTENT_CHARS:
            return content
        return content[:_MAX_PERSISTED_CONTENT_CHARS] + "\n\n[Message truncated for storage]"

    @staticmethod
    def _metadata_json(metadata: dict[str, Any] | None) -> str:
        if not metadata:
            return "{}"
        return json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _parse_metadata(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> WebChatMessage:
        file_name = row["file_name"]
        file = None
        if file_name:
            file = WebChatFile(
                name=str(file_name),
                path=str(row["file_path"]) if row["file_path"] else None,
                caption=str(row["file_caption"] or ""),
            )
        return WebChatMessage(
            id=int(row["id"]),
            session_id=int(row["session_id"]),
            user_id=str(row["user_id"]),
            role=str(row["role"]),
            content=str(row["content"]),
            tone=str(row["tone"]) if row["tone"] is not None else None,
            request_id=str(row["request_id"]) if row["request_id"] is not None else None,
            created_at=str(row["created_at"] or ""),
            metadata=WebChatStore._parse_metadata(row["metadata_json"]),
            file=file,
        )

    @staticmethod
    def _sync_ensure_active_session_id(conn: sqlite3.Connection, user_id: str) -> int:
        conn.execute(
            "INSERT OR IGNORE INTO web_chat_sessions (user_id) VALUES (?)",
            (str(user_id),),
        )
        row = conn.execute(
            """
            SELECT id FROM web_chat_sessions
            WHERE user_id = ? AND ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(user_id),),
        ).fetchone()
        if row is None:
            raise StorageError(f"Failed to create active web chat session for user {user_id}")
        return int(row[0])

    @staticmethod
    def _sync_delete_sessions(conn: sqlite3.Connection, session_ids: list[int]) -> int:
        if not session_ids:
            return 0
        placeholders = ",".join("?" for _ in session_ids)
        conn.execute(
            f"DELETE FROM web_chat_messages WHERE session_id IN ({placeholders})",  # noqa: S608
            session_ids,
        )
        cursor = conn.execute(
            f"DELETE FROM web_chat_sessions WHERE id IN ({placeholders})",  # noqa: S608
            session_ids,
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _sync_prune_active_messages(
        conn: sqlite3.Connection,
        *,
        session_id: int,
        max_messages: int,
    ) -> int:
        rows = conn.execute(
            """
            SELECT id FROM web_chat_messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT -1 OFFSET ?
            """,
            (session_id, max(1, max_messages)),
        ).fetchall()
        ids = [int(row[0]) for row in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = conn.execute(
            f"DELETE FROM web_chat_messages WHERE id IN ({placeholders})",  # noqa: S608
            ids,
        )
        return int(cursor.rowcount or 0)

    def _sync_ensure_active_session(self, user_id: str) -> int:
        try:
            with db_connect(self.db_path) as conn:
                return self._sync_ensure_active_session_id(conn, user_id)
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"Failed to ensure web chat session for user {user_id}: {e}") from e

    async def ensure_active_session(self, user_id: str) -> int:
        """Return the current active web transcript session for a user."""
        return await run_in_thread(self._sync_ensure_active_session, str(user_id))

    def _sync_reset_session(self, user_id: str, reason: str) -> int:
        try:
            with db_connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE web_chat_sessions
                    SET ended_at = CURRENT_TIMESTAMP, reset_reason = ?
                    WHERE user_id = ? AND ended_at IS NULL
                    """,
                    (reason, str(user_id)),
                )
                conn.execute(
                    "INSERT INTO web_chat_sessions (user_id) VALUES (?)",
                    (str(user_id),),
                )
                row = conn.execute("SELECT last_insert_rowid()").fetchone()
                if row is None:
                    raise StorageError("Failed to get inserted web chat session id")
                return int(row[0])
        except Exception as e:
            raise StorageError(f"Failed to reset web chat session for user {user_id}: {e}") from e

    async def reset_session(self, user_id: str, reason: str = "/new") -> int:
        """Archive the current web transcript session and create a new one."""
        return await run_in_thread(self._sync_reset_session, str(user_id), reason)

    def _sync_append_message(
        self,
        *,
        user_id: str,
        role: str,
        content: str,
        tone: str | None,
        request_id: str | None,
        metadata: dict[str, Any] | None,
        file: WebChatFile | None,
    ) -> WebChatMessage:
        if role not in {"user", "assistant", "system"}:
            raise StorageError(f"Unsupported web chat role: {role}")
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                session_id = self._sync_ensure_active_session_id(conn, user_id)
                cursor = conn.execute(
                    """
                    INSERT INTO web_chat_messages (
                        session_id, user_id, role, content, tone, request_id,
                        metadata_json, file_name, file_path, file_caption
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        str(user_id),
                        role,
                        self._clean_content(content),
                        tone,
                        request_id,
                        self._metadata_json(metadata),
                        file.name if file else None,
                        file.path if file else None,
                        file.caption if file else None,
                    ),
                )
                if cursor.lastrowid is None:
                    raise StorageError("Failed to get inserted web chat message id")
                message_id = int(cursor.lastrowid)
                self._sync_prune_active_messages(
                    conn,
                    session_id=session_id,
                    max_messages=self._active_max_messages,
                )
                row = conn.execute(
                    "SELECT * FROM web_chat_messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
                if row is None:
                    raise StorageError(f"Failed to reload web chat message {message_id}")
                return self._message_from_row(row)
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"Failed to append web chat message for user {user_id}: {e}") from e

    async def append_message(
        self,
        *,
        user_id: str,
        role: str,
        content: str,
        tone: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        file: WebChatFile | None = None,
    ) -> WebChatMessage:
        """Persist a user-visible web chat message."""
        return await run_in_thread(
            self._sync_append_message,
            user_id=str(user_id),
            role=role,
            content=content,
            tone=tone,
            request_id=request_id,
            metadata=metadata,
            file=file,
        )

    # ------------------------------------------------------------------
    # Etap 2: multi-chat session management.
    #
    # The active-session invariant stays "one active session per user total"
    # (the partial unique index idx_web_chat_sessions_active is unchanged).
    # `section` (chat|work) is a tag on a session used to filter the sidebar
    # list, NOT a separate active-session slot. Activating a chat archives the
    # currently-active one and reopens the selected one.
    # ------------------------------------------------------------------

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> ChatSessionSummary:
        return ChatSessionSummary(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            section=str(row["section"]),
            title=row["title"] if row["title"] is not None else None,
            created_at=str(row["created_at"]),
            is_active=bool(row["is_active"]),
            msg_count=int(row["msg_count"]),
        )

    def _sync_list_sessions(self, user_id: str, section: str | None) -> list[ChatSessionSummary]:
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if section is None:
                    rows = conn.execute(
                        """
                        SELECT
                            s.id, s.user_id, s.section, s.title, s.created_at,
                            (s.ended_at IS NULL) AS is_active,
                            (SELECT COUNT(*) FROM web_chat_messages m
                             WHERE m.session_id = s.id) AS msg_count
                        FROM web_chat_sessions s
                        WHERE s.user_id = ?
                        ORDER BY is_active DESC, s.id DESC
                        """,
                        (str(user_id),),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT
                            s.id, s.user_id, s.section, s.title, s.created_at,
                            (s.ended_at IS NULL) AS is_active,
                            (SELECT COUNT(*) FROM web_chat_messages m
                             WHERE m.session_id = s.id) AS msg_count
                        FROM web_chat_sessions s
                        WHERE s.user_id = ? AND s.section = ?
                        ORDER BY is_active DESC, s.id DESC
                        """,
                        (str(user_id), section),
                    ).fetchall()
                return [self._session_from_row(row) for row in rows]
        except Exception as e:
            raise StorageError(f"Failed to list web chat sessions for {user_id}: {e}") from e

    async def list_sessions(
        self, user_id: str, *, section: str | None = None
    ) -> list[ChatSessionSummary]:
        """Return the user's chat sessions, optionally filtered by section.

        Active session sorts first (so it stays on top of the sidebar list).
        """
        return await run_in_thread(self._sync_list_sessions, str(user_id), section)

    def _sync_get_session(self, user_id: str, session_id: int) -> ChatSessionSummary | None:
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT
                        s.id, s.user_id, s.section, s.title, s.created_at,
                        (s.ended_at IS NULL) AS is_active,
                        (SELECT COUNT(*) FROM web_chat_messages m
                         WHERE m.session_id = s.id) AS msg_count
                    FROM web_chat_sessions s
                    WHERE s.id = ? AND s.user_id = ?
                    """,
                    (int(session_id), str(user_id)),
                ).fetchone()
                return self._session_from_row(row) if row is not None else None
        except Exception as e:
            raise StorageError(f"Failed to get web chat session {session_id}: {e}") from e

    async def get_session(self, user_id: str, session_id: int) -> ChatSessionSummary | None:
        """Return a single chat session if owned by the user, else None."""
        return await run_in_thread(self._sync_get_session, str(user_id), int(session_id))

    def _sync_create_session(self, user_id: str, section: str) -> int:
        if section not in {"chat", "work"}:
            section = "chat"
        try:
            with db_connect(self.db_path) as conn:
                # Close the currently-active session (if any) so the new one is
                # the single active session per the unique-index invariant.
                conn.execute(
                    """
                    UPDATE web_chat_sessions
                    SET ended_at = CURRENT_TIMESTAMP, reset_reason = 'new_chat'
                    WHERE user_id = ? AND ended_at IS NULL
                    """,
                    (str(user_id),),
                )
                conn.execute(
                    "INSERT INTO web_chat_sessions (user_id, section) VALUES (?, ?)",
                    (str(user_id), section),
                )
                row = conn.execute("SELECT last_insert_rowid()").fetchone()
                if row is None:
                    raise StorageError("Failed to get inserted web chat session id")
                return int(row[0])
        except Exception as e:
            raise StorageError(f"Failed to create web chat session for {user_id}: {e}") from e

    async def create_session(self, user_id: str, *, section: str = "chat") -> int:
        """Create a fresh active chat in the given section, archiving the prior active one."""
        return await run_in_thread(self._sync_create_session, str(user_id), section)

    def _sync_activate_session(self, user_id: str, session_id: int) -> int | None:
        """Make `session_id` the active chat. Returns its id, or None if not owned/found.

        Archives the currently-active session first (preserving the one-active-per-user
        invariant), then reopens the requested one by clearing its ended_at.
        """
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                owned = conn.execute(
                    "SELECT 1 FROM web_chat_sessions WHERE id = ? AND user_id = ?",
                    (int(session_id), str(user_id)),
                ).fetchone()
                if owned is None:
                    return None
                # Close the current active session (could be the same row; harmless).
                conn.execute(
                    """
                    UPDATE web_chat_sessions
                    SET ended_at = CURRENT_TIMESTAMP, reset_reason = 'switched'
                    WHERE user_id = ? AND ended_at IS NULL AND id != ?
                    """,
                    (str(user_id), int(session_id)),
                )
                # Reopen the requested session as active.
                conn.execute(
                    """
                    UPDATE web_chat_sessions
                    SET ended_at = NULL, reset_reason = NULL
                    WHERE id = ? AND user_id = ?
                    """,
                    (int(session_id), str(user_id)),
                )
                return int(session_id)
        except Exception as e:
            raise StorageError(
                f"Failed to activate web chat session {session_id} for {user_id}: {e}"
            ) from e

    async def activate_session(self, user_id: str, session_id: int) -> int | None:
        """Activate a chat session owned by the user. Returns its id, or None if not found."""
        return await run_in_thread(self._sync_activate_session, str(user_id), int(session_id))

    def _sync_set_session_title(self, user_id: str, session_id: int, title: str | None) -> bool:
        try:
            with db_connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    UPDATE web_chat_sessions
                    SET title = ?
                    WHERE id = ? AND user_id = ? AND title IS NULL
                    """,
                    (title, int(session_id), str(user_id)),
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.warning("Failed to set web chat title for session %s: %s", session_id, e)
            return False

    async def set_session_title(self, user_id: str, session_id: int, title: str | None) -> bool:
        """Set title only if currently NULL (auto-naming guard). Returns whether updated."""
        return await run_in_thread(
            self._sync_set_session_title, str(user_id), int(session_id), title
        )

    def _sync_list_messages(self, user_id: str, session_id: int, limit: int) -> WebChatPage:
        """Read messages of a specific (not necessarily active) session. Read-only viewing."""
        limit = max(1, min(limit, 200))
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                owned = conn.execute(
                    "SELECT 1 FROM web_chat_sessions WHERE id = ? AND user_id = ?",
                    (int(session_id), str(user_id)),
                ).fetchone()
                if owned is None:
                    return WebChatPage(session_id=int(session_id), messages=[], has_more=False)
                rows = conn.execute(
                    """
                    SELECT * FROM web_chat_messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(session_id), limit + 1),
                ).fetchall()
                has_more = len(rows) > limit
                selected = rows[:limit]
                messages = [self._message_from_row(row) for row in reversed(selected)]
                return WebChatPage(session_id=int(session_id), messages=messages, has_more=has_more)
        except Exception as e:
            raise StorageError(
                f"Failed to list messages for session {session_id} ({user_id}): {e}"
            ) from e

    async def list_messages(
        self,
        user_id: str,
        *,
        session_id: int,
        limit: int = _DEFAULT_HISTORY_LIMIT,
    ) -> WebChatPage:
        """Return messages of a specific chat session (read-only viewing of any owned chat)."""
        return await run_in_thread(self._sync_list_messages, str(user_id), int(session_id), limit)

    def _sync_list_recent(self, user_id: str, limit: int) -> WebChatPage:
        limit = max(1, min(limit, 200))
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                session_id = self._sync_ensure_active_session_id(conn, user_id)
                rows = conn.execute(
                    """
                    SELECT * FROM web_chat_messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (session_id, limit + 1),
                ).fetchall()
                has_more = len(rows) > limit
                selected = rows[:limit]
                messages = [self._message_from_row(row) for row in reversed(selected)]
                return WebChatPage(session_id=session_id, messages=messages, has_more=has_more)
        except Exception as e:
            raise StorageError(f"Failed to list web chat history for user {user_id}: {e}") from e

    async def list_recent(self, user_id: str, limit: int = _DEFAULT_HISTORY_LIMIT) -> WebChatPage:
        """Return the latest messages from the active web transcript session."""
        return await run_in_thread(self._sync_list_recent, str(user_id), limit)

    def _sync_list_before(self, user_id: str, before_id: int, limit: int) -> WebChatPage:
        limit = max(1, min(limit, 200))
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                session_id = self._sync_ensure_active_session_id(conn, user_id)
                rows = conn.execute(
                    """
                    SELECT * FROM web_chat_messages
                    WHERE session_id = ? AND id < ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (session_id, before_id, limit + 1),
                ).fetchall()
                has_more = len(rows) > limit
                selected = rows[:limit]
                messages = [self._message_from_row(row) for row in reversed(selected)]
                return WebChatPage(session_id=session_id, messages=messages, has_more=has_more)
        except Exception as e:
            raise StorageError(
                f"Failed to list older web chat history for user {user_id}: {e}"
            ) from e

    async def list_before(
        self,
        user_id: str,
        before_id: int,
        limit: int = _DEFAULT_HISTORY_LIMIT,
    ) -> WebChatPage:
        """Return older transcript messages before a known message id."""
        return await run_in_thread(self._sync_list_before, str(user_id), before_id, limit)

    def _sync_backfill_from_memory(self, user_id: str, limit: int) -> int:
        limit = max(1, min(limit, 200))
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                session_id = self._sync_ensure_active_session_id(conn, user_id)
                existing = conn.execute(
                    "SELECT 1 FROM web_chat_messages WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
                if existing is not None:
                    return 0
                try:
                    rows = conn.execute(
                        """
                        SELECT role, content FROM messages
                        WHERE user_id = ?
                          AND role IN ('user', 'assistant')
                          AND content NOT LIKE '[Conversation summary]%'
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (str(user_id), limit),
                    ).fetchall()
                except sqlite3.OperationalError as e:
                    if "no such table" in str(e).lower():
                        return 0
                    raise
                inserted = 0
                for row in reversed(rows):
                    conn.execute(
                        """
                        INSERT INTO web_chat_messages (
                            session_id, user_id, role, content, metadata_json
                        )
                        VALUES (?, ?, ?, ?, '{}')
                        """,
                        (
                            session_id,
                            str(user_id),
                            str(row["role"]),
                            self._clean_content(str(row["content"])),
                        ),
                    )
                    inserted += 1
                return inserted
        except Exception as e:
            raise StorageError(
                f"Failed to backfill web chat history for user {user_id}: {e}"
            ) from e

    async def backfill_from_memory(self, user_id: str, limit: int = 100) -> int:
        """Import visible legacy agent-memory turns into an empty web transcript."""
        return await run_in_thread(self._sync_backfill_from_memory, str(user_id), limit)

    def _sync_latest_usage(self, user_id: str) -> dict[str, Any] | None:
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                session_id = self._sync_ensure_active_session_id(conn, user_id)
                rows = conn.execute(
                    """
                    SELECT metadata_json FROM web_chat_messages
                    WHERE session_id = ? AND role = 'assistant'
                    ORDER BY id DESC
                    LIMIT 50
                    """,
                    (session_id,),
                ).fetchall()
                for row in rows:
                    metadata = self._parse_metadata(row["metadata_json"])
                    usage = metadata.get("usage")
                    if isinstance(usage, dict):
                        return cast(dict[str, Any], usage)
                return None
        except Exception as e:
            raise StorageError(
                f"Failed to load latest web chat usage for user {user_id}: {e}"
            ) from e

    async def latest_usage(self, user_id: str) -> dict[str, Any] | None:
        """Return the most recent context usage snapshot stored in the transcript."""
        return await run_in_thread(self._sync_latest_usage, str(user_id))

    def _sync_latest_file_messages(self, user_id: str, limit: int) -> list[WebChatMessage]:
        limit = max(1, min(limit, 20))
        try:
            with db_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                session_id = self._sync_ensure_active_session_id(conn, user_id)
                rows = conn.execute(
                    """
                    SELECT * FROM web_chat_messages
                    WHERE session_id = ? AND file_name IS NOT NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (session_id, limit),
                ).fetchall()
                return [self._message_from_row(row) for row in rows]
        except Exception as e:
            raise StorageError(
                f"Failed to load latest web file messages for user {user_id}: {e}"
            ) from e

    async def latest_file_messages(
        self,
        user_id: str,
        limit: int = 8,
    ) -> list[WebChatMessage]:
        """Return recent user-visible file artifacts from the active transcript."""
        return await run_in_thread(self._sync_latest_file_messages, str(user_id), limit)

    def _sync_prune_retention(
        self,
        *,
        archived_session_ttl_days: int,
        max_archived_sessions_per_user: int,
    ) -> int:
        ttl_days = max(0, archived_session_ttl_days)
        max_archived = max(0, max_archived_sessions_per_user)
        try:
            with db_connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT id FROM web_chat_sessions
                    WHERE ended_at IS NOT NULL
                      AND ended_at < datetime('now', ?)
                    """,
                    (f"-{ttl_days} days",),
                ).fetchall()
                session_ids = [int(row[0]) for row in rows]

                user_rows = conn.execute(
                    """
                    SELECT DISTINCT user_id FROM web_chat_sessions
                    WHERE ended_at IS NOT NULL
                    """
                ).fetchall()
                for user_row in user_rows:
                    archived = conn.execute(
                        """
                        SELECT id FROM web_chat_sessions
                        WHERE user_id = ? AND ended_at IS NOT NULL
                        ORDER BY id DESC
                        LIMIT -1 OFFSET ?
                        """,
                        (str(user_row[0]), max_archived),
                    ).fetchall()
                    session_ids.extend(int(row[0]) for row in archived)

                unique_ids = sorted(set(session_ids))
                return self._sync_delete_sessions(conn, unique_ids)
        except Exception as e:
            raise StorageError(f"Failed to prune web chat retention: {e}") from e

    async def prune_retention(
        self,
        *,
        archived_session_ttl_days: int,
        max_archived_sessions_per_user: int,
    ) -> int:
        """Prune archived web transcript sessions by age and per-user quota."""
        return await run_in_thread(
            self._sync_prune_retention,
            archived_session_ttl_days=archived_session_ttl_days,
            max_archived_sessions_per_user=max_archived_sessions_per_user,
        )
