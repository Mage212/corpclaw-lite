"""LLM request queue — concurrency-limited execution with position tracking.

Bounds the number of concurrent LLM inference requests to match GPU capacity.
Tracks queue positions so callers can notify users about wait times.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

__all__ = [
    "LLMRequestQueue",
    "QueueEntry",
]

logger = logging.getLogger(__name__)

_ROLLING_AVG_WEIGHT = 0.2
_DEFAULT_AVG_SECONDS = 15.0


@dataclass
class QueueEntry:
    """Tracks a single request in the queue."""

    user_id: str
    enqueued_at: float = field(default_factory=time.monotonic)
    last_notified_at: float = field(default_factory=time.monotonic)


class LLMRequestQueue:
    """Concurrency-limited LLM request queue with position tracking.

    Uses an ``asyncio.Semaphore`` to bound concurrent LLM inference requests.
    Maintains a waiting list so callers can report queue position and estimated
    wait time to users.

    The rolling average of request durations is updated after each ``release()``
    and used to estimate wait times for queued users.
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._waiting: list[QueueEntry] = []
        self._active: dict[str, QueueEntry] = {}
        self._avg_request_seconds: float = _DEFAULT_AVG_SECONDS
        self._lock = asyncio.Lock()
        self._max_concurrent = max_concurrent
        logger.info("LLMRequestQueue initialized: max_concurrent=%d", max_concurrent)

    async def acquire(self, user_id: str) -> QueueEntry:
        """Enter the queue and wait for an inference slot.

        Returns a ``QueueEntry`` once the slot is acquired. The entry's
        ``enqueued_at`` records when the request entered the queue (not when
        the slot was acquired).
        """
        entry = QueueEntry(user_id=user_id)
        async with self._lock:
            self._waiting.append(entry)
            pos = len(self._waiting)
        logger.debug("[queue] user=%s entered queue at position %d", user_id, pos)
        await self._semaphore.acquire()
        async with self._lock:
            if entry in self._waiting:
                self._waiting.remove(entry)
            self._active[user_id] = entry
        logger.debug("[queue] user=%s acquired slot", user_id)
        return entry

    async def release(self, user_id: str, elapsed_seconds: float) -> None:
        """Release an inference slot after the LLM call completes.

        Updates the rolling average request duration used for wait estimates.
        """
        async with self._lock:
            self._active.pop(user_id, None)
        if elapsed_seconds > 0:
            self._avg_request_seconds = (
                1 - _ROLLING_AVG_WEIGHT
            ) * self._avg_request_seconds + _ROLLING_AVG_WEIGHT * elapsed_seconds
        self._semaphore.release()
        logger.debug(
            "[queue] user=%s released slot (elapsed=%.1fs, avg=%.1fs)",
            user_id,
            elapsed_seconds,
            self._avg_request_seconds,
        )

    def get_position(self, user_id: str) -> int | None:
        """Return 0-based queue position for *user_id*, or ``None`` if not queued."""
        for i, entry in enumerate(self._waiting):
            if entry.user_id == user_id:
                return i
        return None

    def get_estimated_wait(self, user_id: str) -> float | None:
        """Return estimated wait in seconds for *user_id*, or ``None`` if not queued."""
        pos = self.get_position(user_id)
        if pos is None:
            return None
        return pos * self._avg_request_seconds

    def get_waiting_entries(self) -> list[QueueEntry]:
        """Return a snapshot of all waiting entries (for notification loops)."""
        return list(self._waiting)

    def get_entry(self, user_id: str) -> QueueEntry | None:
        """Return the queue entry for *user_id* if waiting, else ``None``."""
        for entry in self._waiting:
            if entry.user_id == user_id:
                return entry
        return None

    @property
    def queue_length(self) -> int:
        """Number of requests waiting for a slot."""
        return len(self._waiting)

    @property
    def active_count(self) -> int:
        """Number of requests currently using an inference slot."""
        return len(self._active)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent
