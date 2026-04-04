# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
"""SQLite persistence for onboarding state.

Stores progress (current step, raw answers, completion flag) in the same
``users.db`` database used by :class:`UserManager`.  All public methods are
async and delegate blocking SQLite I/O to a thread pool via ``anyio``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import anyio

from corpclaw_lite.utils.db import db_connect

__all__ = [
    "OnboardingState",
    "OnboardingStorage",
]

logger = logging.getLogger(__name__)


@dataclass
class OnboardingState:
    """Tracks onboarding progress for a user."""

    user_id: int
    current_step: int = 0
    answers: dict[str, str] = field(default_factory=dict)
    completed: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class OnboardingStorage:
    """SQLite persistence for onboarding state."""

    def __init__(self, db_path: Path | str = "data/users.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with db_connect(self._db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS onboarding_state (
                    user_id INTEGER PRIMARY KEY,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    answers_json TEXT NOT NULL DEFAULT '{}',
                    completed BOOLEAN NOT NULL DEFAULT FALSE,
                    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME
                )
                """
            )

    # ── Sync helpers ──────────────────────────────────────────────────────────

    def _sync_get(self, user_id: int) -> OnboardingState | None:
        with db_connect(self._db) as conn:
            row = conn.execute(
                "SELECT user_id, current_step, answers_json, completed "
                "FROM onboarding_state WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return OnboardingState(
            user_id=int(row[0]),
            current_step=int(row[1]),
            answers=json.loads(str(row[2])),
            completed=bool(row[3]),
        )

    def _sync_save(self, state: OnboardingState) -> None:
        completed_at = datetime.now(UTC).isoformat() if state.completed else None
        with db_connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO onboarding_state
                    (user_id, current_step, answers_json, completed, completed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    current_step = excluded.current_step,
                    answers_json = excluded.answers_json,
                    completed = excluded.completed,
                    completed_at = excluded.completed_at
                """,
                (
                    state.user_id,
                    state.current_step,
                    json.dumps(state.answers, ensure_ascii=False),
                    state.completed,
                    completed_at,
                ),
            )

    def _sync_reset(self, user_id: int) -> None:
        with db_connect(self._db) as conn:
            conn.execute(
                "DELETE FROM onboarding_state WHERE user_id = ?",
                (user_id,),
            )

    # ── Async API ─────────────────────────────────────────────────────────────

    async def get_state(self, user_id: int) -> OnboardingState | None:
        """Return onboarding state or None if user never started."""
        return await anyio.to_thread.run_sync(partial(self._sync_get, user_id))

    async def get_or_create(self, user_id: int) -> OnboardingState:
        """Return existing state or create a fresh one."""
        state = await self.get_state(user_id)
        if state is not None:
            return state
        state = OnboardingState(user_id=user_id)
        await self.save_state(state)
        return state

    async def save_state(self, state: OnboardingState) -> None:
        """Persist current state (upsert)."""
        await anyio.to_thread.run_sync(partial(self._sync_save, state))

    async def reset(self, user_id: int) -> None:
        """Delete onboarding state so user can re-onboard."""
        await anyio.to_thread.run_sync(partial(self._sync_reset, user_id))
