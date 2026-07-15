"""B-118: SQLite store for scheduled tasks (data/scheduler.db)."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from corpclaw_lite.exceptions import StorageError
from corpclaw_lite.scheduler.models import ScheduledTask, ScheduleSpec
from corpclaw_lite.utils.async_helpers import run_in_thread
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "ACTIVE_COUNT_STATUSES",
    "InsertConflict",
    "SchedulerStore",
]

logger = logging.getLogger(__name__)

ACTIVE_COUNT_STATUSES = frozenset({"pending", "active"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    task_text TEXT NOT NULL,
    schedule_text TEXT NOT NULL,
    schedule_spec TEXT NOT NULL DEFAULT '{}',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    status TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    dedup_key TEXT NOT NULL DEFAULT '',
    next_run_at TEXT,
    last_run_at TEXT,
    last_status TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    accepted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sched_user_status
    ON scheduled_tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_sched_due
    ON scheduled_tasks(status, enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_sched_dedup
    ON scheduled_tasks(user_id, dedup_key);

CREATE TABLE IF NOT EXISTS schedule_run_log (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    skip_reason TEXT,
    run_id TEXT,
    reply_preview TEXT,
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sched_run_task ON schedule_run_log(task_id);
"""

# Additive columns for DBs created before H1 claim fields.
_CLAIM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("claimed_at", "TEXT"),
    ("claim_token", "TEXT"),
)


class InsertConflict(Exception):
    """Raised when limit or dedup blocks a pending insert."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
    try:
        loaded_obj: object = json.loads(row["schedule_spec"] or "{}")
    except json.JSONDecodeError:
        loaded_obj = {}
    spec_raw: dict[str, Any] = {}
    if isinstance(loaded_obj, dict):
        loaded_map: dict[object, object] = cast(dict[object, object], loaded_obj)
        for key_obj, val in loaded_map.items():
            spec_raw[str(key_obj)] = val
    return ScheduledTask(
        id=str(row["id"]),
        user_id=int(row["user_id"]),
        title=str(row["title"]),
        task_text=str(row["task_text"]),
        schedule_text=str(row["schedule_text"]),
        schedule=ScheduleSpec.from_json(spec_raw),
        timezone=str(row["timezone"] or "UTC"),
        status=str(row["status"]),  # type: ignore[arg-type]
        enabled=bool(row["enabled"]),
        dedup_key=str(row["dedup_key"] or ""),
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        last_status=row["last_status"],
        run_count=int(row["run_count"] or 0),
        error_count=int(row["error_count"] or 0),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        accepted_at=row["accepted_at"],
        claimed_at=_row_get(row, "claimed_at"),
        claim_token=_row_get(row, "claim_token"),
    )


def _row_get(row: sqlite3.Row, key: str) -> str | None:
    try:
        val = row[key]
    except (IndexError, KeyError):
        return None
    if val is None:
        return None
    return str(val)


class SchedulerStore:
    """Persistent scheduled_tasks + run log."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            with db_connect(self.db_path) as conn:
                conn.executescript(_SCHEMA)
                cols = {
                    str(r[1]) for r in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
                }
                for name, col_type in _CLAIM_COLUMNS:
                    if name not in cols:
                        conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {name} {col_type}")
        except Exception as e:
            raise StorageError(f"Failed to init scheduler schema: {e}") from e

    def _sync_count_active(self, user_id: int) -> int:
        with db_connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM scheduled_tasks
                WHERE user_id = ? AND status IN ('pending', 'active')
                """,
                (int(user_id),),
            ).fetchone()
            return int(row[0]) if row else 0

    async def count_active(self, user_id: int) -> int:
        return await run_in_thread(self._sync_count_active, int(user_id))

    def _sync_has_dismissed_dedup(self, user_id: int, dedup_key: str) -> bool:
        if not dedup_key:
            return False
        with db_connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM scheduled_tasks
                WHERE user_id = ? AND dedup_key = ? AND status = 'dismissed'
                LIMIT 1
                """,
                (int(user_id), dedup_key),
            ).fetchone()
            return row is not None

    async def has_dismissed_dedup(self, user_id: int, dedup_key: str) -> bool:
        return await run_in_thread(self._sync_has_dismissed_dedup, int(user_id), dedup_key)

    def _sync_insert(self, task: ScheduledTask) -> ScheduledTask:
        now = _utcnow_iso()
        task.created_at = task.created_at or now
        task.updated_at = now
        with db_connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, user_id, title, task_text, schedule_text, schedule_spec,
                    timezone, status, enabled, dedup_key, next_run_at, last_run_at,
                    last_status, run_count, error_count, created_at, updated_at,
                    accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.user_id,
                    task.title,
                    task.task_text,
                    task.schedule_text,
                    json.dumps(task.schedule.to_json(), ensure_ascii=False),
                    task.timezone,
                    task.status,
                    1 if task.enabled else 0,
                    task.dedup_key,
                    task.next_run_at,
                    task.last_run_at,
                    task.last_status,
                    task.run_count,
                    task.error_count,
                    task.created_at,
                    task.updated_at,
                    task.accepted_at,
                ),
            )
        return task

    async def insert(self, task: ScheduledTask) -> ScheduledTask:
        return await run_in_thread(self._sync_insert, task)

    def _sync_get(self, task_id: str, user_id: int | None = None) -> ScheduledTask | None:
        with db_connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if user_id is None:
                row = conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE id = ? AND user_id = ?",
                    (task_id, int(user_id)),
                ).fetchone()
            return _row_to_task(row) if row is not None else None

    async def get(self, task_id: str, user_id: int | None = None) -> ScheduledTask | None:
        return await run_in_thread(self._sync_get, task_id, user_id)

    def _sync_list_for_user(
        self, user_id: int, statuses: list[str] | None = None
    ) -> list[ScheduledTask]:
        with db_connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = conn.execute(
                    f"""
                    SELECT * FROM scheduled_tasks
                    WHERE user_id = ? AND status IN ({placeholders})
                    ORDER BY created_at DESC
                    """,
                    (int(user_id), *statuses),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM scheduled_tasks
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    """,
                    (int(user_id),),
                ).fetchall()
            return [_row_to_task(r) for r in rows]

    async def list_for_user(
        self, user_id: int, *, statuses: list[str] | None = None
    ) -> list[ScheduledTask]:
        return await run_in_thread(self._sync_list_for_user, int(user_id), statuses)

    def _sync_update(self, task: ScheduledTask) -> ScheduledTask:
        task.updated_at = _utcnow_iso()
        with db_connect(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE scheduled_tasks SET
                    title = ?, task_text = ?, schedule_text = ?, schedule_spec = ?,
                    timezone = ?, status = ?, enabled = ?, dedup_key = ?,
                    next_run_at = ?, last_run_at = ?, last_status = ?,
                    run_count = ?, error_count = ?, updated_at = ?, accepted_at = ?,
                    claimed_at = ?, claim_token = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    task.title,
                    task.task_text,
                    task.schedule_text,
                    json.dumps(task.schedule.to_json(), ensure_ascii=False),
                    task.timezone,
                    task.status,
                    1 if task.enabled else 0,
                    task.dedup_key,
                    task.next_run_at,
                    task.last_run_at,
                    task.last_status,
                    task.run_count,
                    task.error_count,
                    task.updated_at,
                    task.accepted_at,
                    task.claimed_at,
                    task.claim_token,
                    task.id,
                    task.user_id,
                ),
            )
            if cur.rowcount == 0:
                raise StorageError(f"Task {task.id} not found for user {task.user_id}")
        return task

    async def update(self, task: ScheduledTask) -> ScheduledTask:
        return await run_in_thread(self._sync_update, task)

    def _sync_list_due(
        self, now_iso: str, stale_before_iso: str, limit: int = 20
    ) -> list[ScheduledTask]:
        """Due tasks: next_run <= now and not claimed (or claim older than stale_before)."""
        with db_connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = 'active' AND enabled = 1
                  AND next_run_at IS NOT NULL AND next_run_at <= ?
                  AND (claimed_at IS NULL OR claimed_at < ?)
                ORDER BY next_run_at ASC
                LIMIT ?
                """,
                (now_iso, stale_before_iso, int(limit)),
            ).fetchall()
            return [_row_to_task(r) for r in rows]

    async def list_due(
        self, now_iso: str, *, stale_before_iso: str, limit: int = 20
    ) -> list[ScheduledTask]:
        return await run_in_thread(self._sync_list_due, now_iso, stale_before_iso, int(limit))

    def _sync_claim_task(
        self,
        task_id: str,
        expected_next_run_at: str,
        now_iso: str,
        claim_token: str,
        stale_before_iso: str,
    ) -> bool:
        """Optimistic claim. Returns True if this caller owns the run."""
        with db_connect(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE scheduled_tasks
                SET claimed_at = ?, claim_token = ?, updated_at = ?
                WHERE id = ?
                  AND status = 'active'
                  AND enabled = 1
                  AND next_run_at = ?
                  AND (claimed_at IS NULL OR claimed_at < ?)
                """,
                (
                    now_iso,
                    claim_token,
                    now_iso,
                    task_id,
                    expected_next_run_at,
                    stale_before_iso,
                ),
            )
            return int(cur.rowcount) == 1

    async def claim_task(
        self,
        task_id: str,
        *,
        expected_next_run_at: str,
        now_iso: str,
        claim_token: str,
        stale_before_iso: str,
    ) -> bool:
        return await run_in_thread(
            self._sync_claim_task,
            task_id,
            expected_next_run_at,
            now_iso,
            claim_token,
            stale_before_iso,
        )

    def _sync_insert_pending_if_allowed(self, task: ScheduledTask, max_n: int) -> ScheduledTask:
        """Atomic limit + dedup check + insert under one connection."""
        now = _utcnow_iso()
        task.created_at = task.created_at or now
        task.updated_at = now
        with db_connect(self.db_path) as conn:
            if task.dedup_key:
                dismissed = conn.execute(
                    """
                    SELECT 1 FROM scheduled_tasks
                    WHERE user_id = ? AND dedup_key = ? AND status = 'dismissed'
                    LIMIT 1
                    """,
                    (int(task.user_id), task.dedup_key),
                ).fetchone()
                if dismissed is not None:
                    raise InsertConflict(
                        "This proposal was dismissed earlier (same fingerprint). "
                        "Change the wording or schedule to propose again."
                    )
                live = conn.execute(
                    """
                    SELECT 1 FROM scheduled_tasks
                    WHERE user_id = ? AND dedup_key = ?
                      AND status IN ('pending', 'active', 'paused')
                    LIMIT 1
                    """,
                    (int(task.user_id), task.dedup_key),
                ).fetchone()
                if live is not None:
                    raise InsertConflict(
                        "A task with the same fingerprint is already pending/active/paused."
                    )
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM scheduled_tasks
                WHERE user_id = ? AND status IN ('pending', 'active')
                """,
                (int(task.user_id),),
            ).fetchone()
            count = int(row[0]) if row else 0
            if count >= int(max_n):
                raise InsertConflict(
                    f"Limit reached: at most {max_n} pending+active tasks per user "
                    f"(currently {count})."
                )
            conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, user_id, title, task_text, schedule_text, schedule_spec,
                    timezone, status, enabled, dedup_key, next_run_at, last_run_at,
                    last_status, run_count, error_count, created_at, updated_at,
                    accepted_at, claimed_at, claim_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.user_id,
                    task.title,
                    task.task_text,
                    task.schedule_text,
                    json.dumps(task.schedule.to_json(), ensure_ascii=False),
                    task.timezone,
                    task.status,
                    1 if task.enabled else 0,
                    task.dedup_key,
                    task.next_run_at,
                    task.last_run_at,
                    task.last_status,
                    task.run_count,
                    task.error_count,
                    task.created_at,
                    task.updated_at,
                    task.accepted_at,
                    task.claimed_at,
                    task.claim_token,
                ),
            )
        return task

    async def insert_pending_if_allowed(self, task: ScheduledTask, *, max_n: int) -> ScheduledTask:
        return await run_in_thread(self._sync_insert_pending_if_allowed, task, int(max_n))

    def _sync_append_run_log(
        self,
        *,
        task_id: str,
        status: str,
        skip_reason: str | None = None,
        run_id: str | None = None,
        reply_preview: str | None = None,
    ) -> None:
        now = _utcnow_iso()
        with db_connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO schedule_run_log (
                    id, task_id, started_at, finished_at, status,
                    skip_reason, run_id, reply_preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    now,
                    now,
                    status,
                    skip_reason,
                    run_id,
                    (reply_preview or "")[:500] or None,
                ),
            )

    async def append_run_log(
        self,
        *,
        task_id: str,
        status: str,
        skip_reason: str | None = None,
        run_id: str | None = None,
        reply_preview: str | None = None,
    ) -> None:
        await run_in_thread(
            self._sync_append_run_log,
            task_id=task_id,
            status=status,
            skip_reason=skip_reason,
            run_id=run_id,
            reply_preview=reply_preview,
        )

    def _sync_expire_pending(self, cutoff_iso: str) -> int:
        with db_connect(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = 'dismissed', updated_at = ?
                WHERE status = 'pending' AND created_at < ?
                """,
                (_utcnow_iso(), cutoff_iso),
            )
            return int(cur.rowcount)

    async def expire_pending(self, cutoff_iso: str) -> int:
        return await run_in_thread(self._sync_expire_pending, cutoff_iso)
