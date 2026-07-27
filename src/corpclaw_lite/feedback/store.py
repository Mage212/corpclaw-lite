"""B-121 / DC-025a: SQLite store for user 👍/👎 feedback labels.

Mirrors the :mod:`scheduler.store` pattern: path-injected constructor, additive
``CREATE TABLE IF NOT EXISTS`` schema, ``db_connect`` context manager (FK on,
busy_timeout), ``run_in_thread`` async wrappers.

The store is the single source of truth for which runs a user has voted on
and how. It is keyed by ``(run_id, user_id)`` with UPSERT semantics so a user
can change their mind when ``allow_change`` is True (the default).

Each label's ``run_id`` is the JOIN key against ``logs/llm_payloads.jsonl`` —
the dataset exporter (B-122) reads both files and correlates them offline.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from corpclaw_lite.exceptions import StorageError
from corpclaw_lite.feedback.models import FeedbackChannel, FeedbackLabel, FeedbackRating
from corpclaw_lite.utils.async_helpers import run_in_thread
from corpclaw_lite.utils.db import db_connect

__all__ = [
    "FeedbackStore",
    "VALID_RATINGS",
    "VALID_CHANNELS",
]

logger = logging.getLogger(__name__)

# Closed sets — the rating/channel columns are TEXT (no CHECK constraint) for
# migration-friendliness, but the store rejects anything outside these on write.
VALID_RATINGS: frozenset[str] = frozenset({"up", "down"})
VALID_CHANNELS: frozenset[str] = frozenset({"telegram", "web"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback_labels (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    rating TEXT NOT NULL,
    channel TEXT NOT NULL,
    message_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_run ON feedback_labels(run_id);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback_labels(user_id, created_at);
"""


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _row_to_label(row: sqlite3.Row) -> FeedbackLabel:
    return FeedbackLabel(
        run_id=str(row["run_id"]),
        user_id=str(row["user_id"]),
        rating=str(row["rating"]),
        channel=str(row["channel"]),
        message_ref=str(row["message_ref"]) if row["message_ref"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class FeedbackStore:
    """Persistent feedback_labels table.

    Thread-safe via ``db_connect`` (each call opens a fresh connection under a
    context manager). Async methods delegate to ``_sync_*`` via ``run_in_thread``
    to avoid blocking the event loop.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            with db_connect(self.db_path) as conn:
                conn.executescript(_SCHEMA)
        except Exception as e:
            raise StorageError(f"Failed to init feedback schema: {e}") from e

    def _sync_record(
        self,
        *,
        run_id: str,
        user_id: str,
        rating: FeedbackRating,
        channel: FeedbackChannel,
        message_ref: str | None,
        allow_change: bool,
    ) -> FeedbackLabel:
        """Insert or update a label. Returns the persisted label.

        - First vote on (run_id, user_id): always inserts.
        - Subsequent vote with ``allow_change=True``: UPSERT (rating/channel/
          message_ref/updated_at overwritten).
        - Subsequent vote with ``allow_change=False``: no-op, returns the
          existing row unchanged.
        """
        now = _utcnow_iso()
        with db_connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT rating, channel, message_ref, created_at, updated_at "
                "FROM feedback_labels WHERE run_id = ? AND user_id = ?",
                (run_id, user_id),
            ).fetchone()
            if existing is not None and not allow_change:
                return FeedbackLabel(
                    run_id=run_id,
                    user_id=user_id,
                    rating=str(existing["rating"]),
                    channel=str(existing["channel"]),
                    message_ref=(
                        str(existing["message_ref"])
                        if existing["message_ref"] is not None
                        else None
                    ),
                    created_at=str(existing["created_at"]),
                    updated_at=str(existing["updated_at"]),
                )
            if existing is None:
                # INSERT path.
                conn.execute(
                    "INSERT INTO feedback_labels "
                    "(id, run_id, user_id, rating, channel, message_ref, "
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        run_id,
                        user_id,
                        rating,
                        channel,
                        message_ref,
                        now,
                        now,
                    ),
                )
                return FeedbackLabel(
                    run_id=run_id,
                    user_id=user_id,
                    rating=rating,
                    channel=channel,
                    message_ref=message_ref,
                    created_at=now,
                    updated_at=now,
                )
            # UPSERT path — allow_change=True with an existing row.
            conn.execute(
                "UPDATE feedback_labels SET rating = ?, channel = ?, "
                "message_ref = ?, updated_at = ? "
                "WHERE run_id = ? AND user_id = ?",
                (rating, channel, message_ref, now, run_id, user_id),
            )
            return FeedbackLabel(
                run_id=run_id,
                user_id=user_id,
                rating=rating,
                channel=channel,
                message_ref=message_ref,
                created_at=str(existing["created_at"]),
                updated_at=now,
            )

    async def record(
        self,
        *,
        run_id: str,
        user_id: str,
        rating: FeedbackRating,
        channel: FeedbackChannel,
        message_ref: str | None = None,
        allow_change: bool = True,
    ) -> FeedbackLabel:
        """Insert or update a feedback label for ``(run_id, user_id)``."""
        if rating not in VALID_RATINGS:
            raise ValueError(f"Invalid rating {rating!r}; expected one of {sorted(VALID_RATINGS)}")
        if channel not in VALID_CHANNELS:
            raise ValueError(
                f"Invalid channel {channel!r}; expected one of {sorted(VALID_CHANNELS)}"
            )
        return await run_in_thread(
            self._sync_record,
            run_id=run_id,
            user_id=user_id,
            rating=rating,
            channel=channel,
            message_ref=message_ref,
            allow_change=allow_change,
        )

    def _sync_get(self, run_id: str, user_id: str) -> FeedbackLabel | None:
        with db_connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT run_id, user_id, rating, channel, message_ref, "
                "created_at, updated_at FROM feedback_labels "
                "WHERE run_id = ? AND user_id = ?",
                (run_id, user_id),
            ).fetchone()
            return _row_to_label(row) if row is not None else None

    async def get(self, run_id: str, user_id: str) -> FeedbackLabel | None:
        """Return the user's current label for ``run_id``, or None."""
        return await run_in_thread(self._sync_get, run_id, user_id)

    def _sync_list_for_run(self, run_id: str) -> list[FeedbackLabel]:
        with db_connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT run_id, user_id, rating, channel, message_ref, "
                "created_at, updated_at FROM feedback_labels WHERE run_id = ? "
                "ORDER BY created_at",
                (run_id,),
            ).fetchall()
            return [_row_to_label(r) for r in rows]

    async def list_for_run(self, run_id: str) -> list[FeedbackLabel]:
        """All labels across users for one run (for the dataset exporter)."""
        return await run_in_thread(self._sync_list_for_run, run_id)
