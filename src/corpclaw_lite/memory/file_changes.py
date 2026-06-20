"""File-change journal DAO (B-040).

Two SQLite tables keyed by ``(user_id, run_id)`` track every mutation an
office tool made to a workspace file, so the user (and the agent) can answer
"what did the agent change?" and revert it. Backups of the pre-write bytes
live on disk in ``.snapshots/<run_id>/`` (handled by
:class:`corpclaw_lite.agent.file_snapshots.FileSnapshotStore`); this DAO keeps
only the metadata.

Schema is adapted from OpenCowork ``agent_change_sets`` / ``agent_file_changes``
but stores backup bytes on disk instead of inline JSON content (we work with
binary office formats — xlsx/docx — where text snapshots are meaningless).

The DAO follows the established pattern of :class:`WebChatStore`: a class with
its own ``_init_db``, sharing ``memory.db`` (WAL), with synchronous ``_sync_*``
helpers wrapped in ``anyio.to_thread`` by async public methods.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpclaw_lite.exceptions import StorageError
from corpclaw_lite.paths import DATA_DIR
from corpclaw_lite.utils.async_helpers import run_in_thread
from corpclaw_lite.utils.db import db_connect

if TYPE_CHECKING:
    pass

__all__ = [
    "FileChange",
    "FileChangeDAO",
]

logger = logging.getLogger(__name__)

# Retention for fully-reverted change sets (both DB rows and on-disk backups).
# OpenCowork uses 7 days; same default here.
_FINALIZED_RETENTION_SECONDS = 7 * 24 * 3600


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class FileChange:
    """One recorded file mutation (one row in ``agent_file_changes``)."""

    id: str
    run_id: str
    user_id: str
    tool_name: str
    file_path: str
    op: str  # 'create' | 'modify'
    status: str  # 'open' | 'reverted'
    before_hash: str | None
    after_hash: str
    backup_path: str | None
    size_bytes: int
    created_at: int
    reverted_at: int | None
    sort_order: int


class FileChangeDAO:
    """Persistent file-change journal for office tools (B-040)."""

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
                    CREATE TABLE IF NOT EXISTS agent_change_sets (
                        run_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_file_changes (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        op TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        before_hash TEXT,
                        after_hash TEXT NOT NULL,
                        backup_path TEXT,
                        size_bytes INTEGER,
                        created_at INTEGER NOT NULL,
                        reverted_at INTEGER,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        FOREIGN KEY (run_id)
                            REFERENCES agent_change_sets(run_id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_agent_file_changes_run "
                    "ON agent_file_changes(run_id, sort_order)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_agent_file_changes_user_recent "
                    "ON agent_file_changes(user_id, created_at)"
                )
        except Exception as exc:
            logger.error("Failed to initialise file_changes tables: %s", exc)
            raise StorageError("file_changes init failed") from exc

    # ─── record ──────────────────────────────────────────────────────────────

    def _sync_record_change(
        self,
        *,
        user_id: str,
        run_id: str,
        tool_name: str,
        file_path: str,
        op: str,
        before_hash: str | None,
        after_hash: str,
        backup_path: str | None,
        size_bytes: int,
    ) -> str | None:
        """Insert one change row. Returns the new change_id, or None if the
        result is a no-op (before_hash == after_hash — hash-dedup)."""
        if before_hash is not None and before_hash == after_hash:
            return None

        now = _now_ms()
        with db_connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_change_sets (run_id, user_id, status, created_at, updated_at)
                VALUES (?, ?, 'open', ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (run_id, user_id, now, now),
            )
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) FROM agent_file_changes WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            next_seq = (row[0] if row else -1) + 1
            change_id = f"{run_id}:{next_seq}"
            conn.execute(
                """
                INSERT INTO agent_file_changes
                    (id, run_id, user_id, tool_name, file_path, op, status,
                     before_hash, after_hash, backup_path, size_bytes,
                     created_at, reverted_at, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    change_id,
                    run_id,
                    user_id,
                    tool_name,
                    file_path,
                    op,
                    before_hash,
                    after_hash,
                    backup_path,
                    size_bytes,
                    now,
                    next_seq,
                ),
            )
        return change_id

    async def record_change(
        self,
        *,
        user_id: str,
        run_id: str,
        tool_name: str,
        file_path: str,
        op: str,
        before_hash: str | None,
        after_hash: str,
        backup_path: str | None,
        size_bytes: int,
    ) -> str | None:
        return await run_in_thread(
            self._sync_record_change,
            user_id=user_id,
            run_id=run_id,
            tool_name=tool_name,
            file_path=file_path,
            op=op,
            before_hash=before_hash,
            after_hash=after_hash,
            backup_path=backup_path,
            size_bytes=size_bytes,
        )

    # ─── read ────────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_change(row: Any) -> FileChange:
        return FileChange(
            id=row[0],
            run_id=row[1],
            user_id=row[2],
            tool_name=row[3],
            file_path=row[4],
            op=row[5],
            status=row[6],
            before_hash=row[7],
            after_hash=row[8],
            backup_path=row[9],
            size_bytes=row[10] if row[10] is not None else 0,
            created_at=row[11],
            reverted_at=row[12],
            sort_order=row[13],
        )

    _LIST_FOR_RUN_SQL = (
        "SELECT id, run_id, user_id, tool_name, file_path, op, status, "
        "before_hash, after_hash, backup_path, size_bytes, created_at, "
        "reverted_at, sort_order "
        "FROM agent_file_changes WHERE run_id = ? ORDER BY sort_order ASC"
    )

    _LIST_RECENT_SQL = (
        "SELECT id, run_id, user_id, tool_name, file_path, op, status, "
        "before_hash, after_hash, backup_path, size_bytes, created_at, "
        "reverted_at, sort_order "
        "FROM agent_file_changes WHERE user_id = ? "
        "ORDER BY created_at DESC LIMIT ?"
    )

    def _sync_list_for_run(self, run_id: str) -> list[FileChange]:
        with db_connect(self.db_path) as conn:
            rows = conn.execute(self._LIST_FOR_RUN_SQL, (run_id,)).fetchall()
        return [self._row_to_change(r) for r in rows]

    async def list_for_run(self, run_id: str) -> list[FileChange]:
        return await run_in_thread(self._sync_list_for_run, run_id)

    def _sync_list_recent_for_user(self, user_id: str, limit: int) -> list[FileChange]:
        with db_connect(self.db_path) as conn:
            rows = conn.execute(self._LIST_RECENT_SQL, (user_id, limit)).fetchall()
        return [self._row_to_change(r) for r in rows]

    async def list_recent_for_user(self, user_id: str, *, limit: int = 3) -> list[FileChange]:
        return await run_in_thread(self._sync_list_recent_for_user, user_id, limit)

    # ─── revert ──────────────────────────────────────────────────────────────

    def _sync_mark_reverted(self, run_id: str, change_id: str) -> bool:
        """Mark one change reverted; returns True if a row was updated."""
        now = _now_ms()
        with db_connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE agent_file_changes SET status='reverted', reverted_at=? "
                "WHERE id=? AND status='open'",
                (now, change_id),
            )
            updated = cur.rowcount > 0
            if updated:
                self._recompute_run_status(conn, run_id, now)
        return updated

    async def mark_reverted(self, run_id: str, change_id: str) -> bool:
        return await run_in_thread(self._sync_mark_reverted, run_id, change_id)

    @staticmethod
    def _recompute_run_status(conn: Any, run_id: str, now: int) -> None:
        """A run is 'reverted' once all of its changes are reverted."""
        row = conn.execute(
            "SELECT COUNT(*) FROM agent_file_changes WHERE run_id=? AND status='open'",
            (run_id,),
        ).fetchone()
        open_count = row[0] if row else 0
        new_status = "reverted" if open_count == 0 else "open"
        conn.execute(
            "UPDATE agent_change_sets SET status=?, updated_at=? WHERE run_id=?",
            (new_status, now, run_id),
        )

    # ─── GC ──────────────────────────────────────────────────────────────────

    def _sync_gc_finalized_before(self, cutoff_ms: int) -> list[str]:
        """Delete reverted change sets older than ``cutoff_ms``.

        Returns the list of run_ids whose rows were removed (caller should
        also prune on-disk backups for these runs).
        """
        with db_connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT run_id FROM agent_change_sets WHERE status='reverted' AND updated_at < ?",
                (cutoff_ms,),
            ).fetchall()
            run_ids = [r[0] for r in rows]
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                conn.execute(
                    f"DELETE FROM agent_file_changes "  # noqa: S608
                    f"WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                conn.execute(
                    f"DELETE FROM agent_change_sets "  # noqa: S608
                    f"WHERE run_id IN ({placeholders})",
                    run_ids,
                )
        return run_ids

    async def gc_finalized_before(self, cutoff_ms: int) -> list[str]:
        return await run_in_thread(self._sync_gc_finalized_before, cutoff_ms)

    async def gc_old_finalized(self) -> list[str]:
        """Convenience: prune runs older than the 7-day retention window."""
        cutoff = _now_ms() - _FINALIZED_RETENTION_SECONDS * 1000
        return await self.gc_finalized_before(cutoff)
