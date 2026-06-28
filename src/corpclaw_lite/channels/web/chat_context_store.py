"""Full LLM-context persistence per chat (B-063, S1).

This store captures the *complete* LLM-facing context for each web chat —
role/content **plus** structured ``tool_calls``, ``tool_call_id``, ``name`` and
``reasoning`` — so that any chat can later be restored to its exact LLM state
(S2) or compressed in-place (S3).

It intentionally lives next to, but separately from:

- ``SQLiteMemory.messages`` — the agent's per-user in-memory context (role/content
  + an audit ``reasoning`` column; no tool_calls/tool_call_id). Switching chats
  clears that table, so it cannot serve as per-chat durable storage.
- ``WebChatStore.web_chat_messages`` — the user-visible transcript
  (role/content/tone + aggregate stats). It has no reasoning/tool_calls columns
  and is append-only/never compacted; it is for UI history, not LLM replay.

``web_chat_context`` is the model-facing mirror: a faithful, ordered,
compactable snapshot of what the model saw, keyed by ``(session_id, seq)``.

All three stores share ``data/memory.db`` (WAL); they never JOIN each other.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from corpclaw_lite.paths import DATA_DIR
from corpclaw_lite.utils.async_helpers import run_in_thread
from corpclaw_lite.utils.db import db_connect

__all__ = ["ChatContextStore"]

logger = logging.getLogger(__name__)


class ChatContextStore:
    """Persistent full-LLM-context store, keyed by ``(session_id, seq)``.

    One row == one message in the LLM-facing context (system/user/assistant/tool),
    including structured ``tool_calls`` and ``reasoning``. ``seq`` is a monotonic
    per-session integer so the exact ordering the model saw can be reconstructed
    deterministically (timestamps are ambiguous under batched inserts).
    """

    def __init__(self, db_path: str | Path = DATA_DIR / "memory.db") -> None:
        self.db_path = Path(db_path)
        self._init_db()

    # ------------------------------------------------------------------
    # Schema

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS web_chat_context (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id   INTEGER NOT NULL,
                        user_id      TEXT NOT NULL,
                        role         TEXT NOT NULL,
                        content      TEXT NOT NULL,
                        tool_calls   TEXT,
                        tool_call_id TEXT,
                        name         TEXT,
                        reasoning    TEXT,
                        seq          INTEGER NOT NULL,
                        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(session_id) REFERENCES web_chat_sessions(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_web_chat_context_session
                    ON web_chat_context(session_id, seq)
                    """
                )
                # ON DELETE CASCADE only fires when foreign_keys are on per-connection.
                conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            logger.exception("Failed to initialize web_chat_context schema")
            raise

    # ------------------------------------------------------------------
    # Write

    async def append_context(
        self,
        *,
        session_id: int,
        user_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        reasoning: str | None = None,
    ) -> int:
        """Append one LLM-facing message to the session's context.

        Returns the assigned ``seq`` (monotonic per session). ``tool_calls`` is
        JSON-serialised (the OpenAI tool_calls schema: id/type/function{name,
        arguments}); other fields are stored verbatim.
        """
        return await run_in_thread(
            self._sync_append_context,
            session_id=int(session_id),
            user_id=str(user_id),
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            name=name,
            reasoning=reasoning,
        )

    def _sync_append_context(
        self,
        *,
        session_id: int,
        user_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None,
        tool_call_id: str | None,
        name: str | None,
        reasoning: str | None,
    ) -> int:
        tool_calls_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        with db_connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM web_chat_context WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO web_chat_context
                    (session_id, user_id, role, content, tool_calls,
                     tool_call_id, name, reasoning, seq)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    role,
                    content,
                    tool_calls_json,
                    tool_call_id,
                    name,
                    reasoning,
                    seq,
                ),
            )
        return int(seq)

    async def replace_context(
        self, *, session_id: int, user_id: str, messages: list[dict[str, Any]]
    ) -> None:
        """Atomically replace a session's full context with ``messages``.

        Clears all rows for the session then re-inserts ``messages`` in order,
        assigning fresh sequential ``seq`` values. Used by S3 compress-any-chat.
        """
        await run_in_thread(
            self._sync_replace_context,
            session_id=int(session_id),
            user_id=str(user_id),
            messages=messages,
        )

    def _sync_replace_context(
        self, *, session_id: int, user_id: str, messages: list[dict[str, Any]]
    ) -> None:
        with db_connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM web_chat_context WHERE session_id = ?", (session_id,))
            for seq, msg in enumerate(messages, start=1):
                role = str(msg.get("role", "user"))
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = json.dumps(content, ensure_ascii=False)
                content = str(content)
                tool_calls = msg.get("tool_calls")
                tool_calls_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
                conn.execute(
                    """
                    INSERT INTO web_chat_context
                        (session_id, user_id, role, content, tool_calls,
                         tool_call_id, name, reasoning, seq)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        user_id,
                        role,
                        content,
                        tool_calls_json,
                        msg.get("tool_call_id"),
                        msg.get("name"),
                        msg.get("reasoning"),
                        seq,
                    ),
                )

    # ------------------------------------------------------------------
    # Read

    async def list_context(self, session_id: int) -> list[dict[str, Any]]:
        """Return the session's LLM-facing context in chronological order."""
        return await run_in_thread(self._sync_list_context, session_id=int(session_id))

    def _sync_list_context(self, *, session_id: int) -> list[dict[str, Any]]:
        with db_connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT role, content, tool_calls, tool_call_id, name, reasoning, seq
                FROM web_chat_context
                WHERE session_id = ?
                ORDER BY seq ASC
                """,
                (session_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            msg: dict[str, Any] = {
                "role": str(row["role"]),  # type: ignore[index]
                "content": str(row["content"]),  # type: ignore[index]
            }
            tc_raw = row["tool_calls"]  # type: ignore[index]
            if tc_raw:
                try:
                    msg["tool_calls"] = json.loads(tc_raw)
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "web_chat_context: malformed tool_calls for session %s", session_id
                    )
            if row["tool_call_id"] is not None:  # type: ignore[index]
                msg["tool_call_id"] = str(row["tool_call_id"])  # type: ignore[index]
            if row["name"] is not None:  # type: ignore[index]
                msg["name"] = str(row["name"])  # type: ignore[index]
            if row["reasoning"] is not None:  # type: ignore[index]
                msg["reasoning"] = str(row["reasoning"])  # type: ignore[index]
            out.append(msg)
        return out

    async def clear_context(self, session_id: int) -> int:
        """Delete all context rows for a session. Returns the deleted count."""
        return await run_in_thread(self._sync_clear_context, session_id=int(session_id))

    def _sync_clear_context(self, *, session_id: int) -> int:
        with db_connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM web_chat_context WHERE session_id = ?", (session_id,))
        return int(cur.rowcount or 0)

    def has_context(self, session_id: int) -> bool:
        """Cheap EXISTS check (synchronous) — used by S2 activate to decide
        whether to load persisted context or fall back to legacy reset."""
        try:
            with db_connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT 1 FROM web_chat_context WHERE session_id = ? LIMIT 1",
                    (int(session_id),),
                ).fetchone()
            return row is not None
        except Exception:
            logger.exception("web_chat_context: has_context failed")
            return False
