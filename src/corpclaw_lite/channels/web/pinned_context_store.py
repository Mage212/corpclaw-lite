"""Durable per-session pinned files for re-inject each turn (B-095).

Pins are NOT stored in the compressible transcript middle — content is loaded
from this store and injected into the prompt assembly every AgentLoop run.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corpclaw_lite.paths import DATA_DIR
from corpclaw_lite.utils.async_helpers import run_in_thread
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "PinnedContextRecord",
    "PinnedContextStore",
    "format_pinned_files_block",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PinnedContextRecord:
    """One pinned file row."""

    session_id: int
    user_id: str
    path: str
    kind: str
    tokens: int
    approximate: bool
    mode: str
    content: str
    label: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "tokens": self.tokens,
            "approximate": self.approximate,
            "mode": self.mode,
            "label": self.label,
        }

    def inject_block(self) -> str:
        flags: list[str] = [f"~{self.tokens} tokens"]
        if self.approximate:
            flags.append("approx")
        if self.mode == "chunked":
            flags.append("chunked")
        meta = ", ".join(flags)
        return f"[Pinned file: {self.path} ({meta})]\n```\n{self.content}\n```"


def format_pinned_files_block(pins: list[PinnedContextRecord]) -> str:
    """Build ``## Pinned Files`` section for prompt assembly."""
    if not pins:
        return ""
    body = "\n\n".join(p.inject_block() for p in pins)
    return f"\n\n## Pinned Files\n{body}"


class PinnedContextStore:
    """SQLite pin store keyed by (session_id, path)."""

    def __init__(self, db_path: str | Path = DATA_DIR / "memory.db") -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with db_connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS web_chat_pins (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL,
                        user_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        tokens INTEGER NOT NULL,
                        approximate INTEGER NOT NULL DEFAULT 1,
                        mode TEXT NOT NULL,
                        content TEXT NOT NULL,
                        label TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(session_id, path),
                        FOREIGN KEY(session_id) REFERENCES web_chat_sessions(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_web_chat_pins_session
                    ON web_chat_pins(session_id)
                    """
                )
                conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            logger.exception("Failed to initialize web_chat_pins schema")
            raise

    def _row_to_record(self, row: sqlite3.Row) -> PinnedContextRecord:
        return PinnedContextRecord(
            session_id=int(row["session_id"]),
            user_id=str(row["user_id"]),
            path=str(row["path"]),
            kind=str(row["kind"]),
            tokens=int(row["tokens"]),
            approximate=bool(row["approximate"]),
            mode=str(row["mode"]),
            content=str(row["content"]),
            label=str(row["label"]),
        )

    def _sync_list(self, session_id: int, user_id: str) -> list[PinnedContextRecord]:
        with db_connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT session_id, user_id, path, kind, tokens, approximate,
                       mode, content, label
                FROM web_chat_pins
                WHERE session_id = ? AND user_id = ?
                ORDER BY created_at ASC, path ASC
                """,
                (int(session_id), str(user_id)),
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    async def list_pins(self, session_id: int, user_id: str) -> list[PinnedContextRecord]:
        return await run_in_thread(self._sync_list, int(session_id), str(user_id))

    def _sync_total_tokens(self, session_id: int, user_id: str) -> int:
        with db_connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            row = conn.execute(
                """
                SELECT COALESCE(SUM(tokens), 0) AS total
                FROM web_chat_pins
                WHERE session_id = ? AND user_id = ?
                """,
                (int(session_id), str(user_id)),
            ).fetchone()
            return int(row[0]) if row is not None else 0

    async def total_tokens(self, session_id: int, user_id: str) -> int:
        return await run_in_thread(self._sync_total_tokens, int(session_id), str(user_id))

    def _sync_get(self, session_id: int, user_id: str, path: str) -> PinnedContextRecord | None:
        with db_connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT session_id, user_id, path, kind, tokens, approximate,
                       mode, content, label
                FROM web_chat_pins
                WHERE session_id = ? AND user_id = ? AND path = ?
                """,
                (int(session_id), str(user_id), path),
            ).fetchone()
            return self._row_to_record(row) if row is not None else None

    async def get_pin(self, session_id: int, user_id: str, path: str) -> PinnedContextRecord | None:
        return await run_in_thread(self._sync_get, int(session_id), str(user_id), path)

    def _sync_upsert(self, record: PinnedContextRecord) -> None:
        with db_connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """
                INSERT INTO web_chat_pins (
                    session_id, user_id, path, kind, tokens, approximate,
                    mode, content, label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, path) DO UPDATE SET
                    kind = excluded.kind,
                    tokens = excluded.tokens,
                    approximate = excluded.approximate,
                    mode = excluded.mode,
                    content = excluded.content,
                    label = excluded.label
                """,
                (
                    record.session_id,
                    record.user_id,
                    record.path,
                    record.kind,
                    record.tokens,
                    1 if record.approximate else 0,
                    record.mode,
                    record.content,
                    record.label,
                ),
            )

    async def upsert_pin(self, record: PinnedContextRecord) -> None:
        await run_in_thread(self._sync_upsert, record)

    def _sync_remove(self, session_id: int, user_id: str, path: str) -> bool:
        with db_connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            cur = conn.execute(
                """
                DELETE FROM web_chat_pins
                WHERE session_id = ? AND user_id = ? AND path = ?
                """,
                (int(session_id), str(user_id), path),
            )
            return cur.rowcount > 0

    async def remove_pin(self, session_id: int, user_id: str, path: str) -> bool:
        return await run_in_thread(self._sync_remove, int(session_id), str(user_id), path)

    def _sync_clear_session(self, session_id: int, user_id: str) -> int:
        with db_connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            cur = conn.execute(
                """
                DELETE FROM web_chat_pins
                WHERE session_id = ? AND user_id = ?
                """,
                (int(session_id), str(user_id)),
            )
            return int(cur.rowcount)

    async def clear_session(self, session_id: int, user_id: str) -> int:
        return await run_in_thread(self._sync_clear_session, int(session_id), str(user_id))

    def public_list(self, pins: list[PinnedContextRecord]) -> list[dict[str, Any]]:
        return [p.to_public_dict() for p in pins]
