"""LLM request queue — concurrency-limited execution with position tracking.

Bounds the number of concurrent LLM inference requests to match GPU capacity.
Tracks queue positions so callers can notify users about wait times.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Literal

from corpclaw_lite.logging import health
from corpclaw_lite.logging.trace import log_event

__all__ = [
    "LLMLoadClass",
    "LLMRequestQueue",
    "QueueEntry",
]

logger = logging.getLogger(__name__)

_ROLLING_AVG_WEIGHT = 0.2
_DEFAULT_AVG_SECONDS = 15.0
LLMLoadClass = Literal[
    "interactive",
    "subagent",
    "vision",
    "compression",
    "consolidation",
    "calibration",
    "maintenance",
]


@dataclass
class QueueEntry:
    """Tracks a single request in the queue."""

    user_id: str
    task_kind: str = "default"
    load_class: LLMLoadClass = "interactive"
    run_id: str | None = None
    enqueued_at: float = field(default_factory=time.monotonic)
    last_notified_at: float = field(default_factory=time.monotonic)
    acquired_at: float | None = None
    queue_position_at_entry: int = 0

    @property
    def queue_wait_seconds(self) -> float | None:
        """Return queue wait duration once acquired."""
        if self.acquired_at is None:
            return None
        return self.acquired_at - self.enqueued_at


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
        self._active: list[QueueEntry] = []
        self._avg_request_seconds: float = _DEFAULT_AVG_SECONDS
        self._lock = asyncio.Lock()
        self._max_concurrent = max_concurrent
        logger.info("LLMRequestQueue initialized: max_concurrent=%d", max_concurrent)

    async def acquire(
        self,
        user_id: str,
        *,
        task_kind: str = "default",
        load_class: LLMLoadClass = "interactive",
        run_id: str | None = None,
    ) -> QueueEntry:
        """Enter the queue and wait for an inference slot.

        Returns a ``QueueEntry`` once the slot is acquired. The entry's
        ``enqueued_at`` records when the request entered the queue (not when
        the slot was acquired).
        """
        entry = QueueEntry(
            user_id=user_id,
            task_kind=task_kind,
            load_class=load_class,
            run_id=run_id,
        )
        acquired = False
        async with self._lock:
            self._waiting.append(entry)
            entry.queue_position_at_entry = len(self._waiting) - 1
            pos = entry.queue_position_at_entry
            waiting_count = len(self._waiting)
            active_count = len(self._active)
        health.increment("llm_queue_entered")
        log_event(
            "llm_queue_entered",
            run_id or "unknown",
            user_id=user_id,
            task_kind=task_kind,
            load_class=load_class,
            queue_position=pos,
            waiting_count=waiting_count,
            active_count=active_count,
            max_concurrent=self._max_concurrent,
        )
        logger.debug(
            "[queue] user=%s entered queue at position %d (%s/%s)",
            user_id,
            pos + 1,
            task_kind,
            load_class,
        )
        try:
            await self._semaphore.acquire()
            acquired = True
            async with self._lock:
                if entry in self._waiting:
                    self._waiting.remove(entry)
                entry.acquired_at = time.monotonic()
                self._active.append(entry)
                waiting_count = len(self._waiting)
                active_count = len(self._active)
        except asyncio.CancelledError:
            async with self._lock:
                if entry in self._waiting:
                    self._waiting.remove(entry)
                waiting_count = len(self._waiting)
                active_count = len(self._active)
            if acquired:
                self._semaphore.release()
            health.increment("llm_queue_cancelled")
            log_event(
                "llm_queue_cancelled",
                run_id or "unknown",
                user_id=user_id,
                task_kind=task_kind,
                load_class=load_class,
                waiting_count=waiting_count,
                active_count=active_count,
                max_concurrent=self._max_concurrent,
            )
            raise
        queue_wait = entry.queue_wait_seconds or 0.0
        health.increment("llm_queue_acquired")
        log_event(
            "llm_queue_acquired",
            run_id or "unknown",
            user_id=user_id,
            task_kind=task_kind,
            load_class=load_class,
            queue_wait_ms=round(queue_wait * 1000, 1),
            waiting_count=waiting_count,
            active_count=active_count,
            max_concurrent=self._max_concurrent,
        )
        logger.debug(
            "[queue] user=%s acquired slot after %.1fs (%s/%s)",
            user_id,
            queue_wait,
            task_kind,
            load_class,
        )
        return entry

    async def release(self, user_id: str | QueueEntry, elapsed_seconds: float) -> None:
        """Release an inference slot after the LLM call completes.

        Updates the rolling average request duration used for wait estimates.
        """
        entry: QueueEntry | None = user_id if isinstance(user_id, QueueEntry) else None
        target_user_id = user_id.user_id if isinstance(user_id, QueueEntry) else user_id
        async with self._lock:
            if entry is None:
                for active_entry in self._active:
                    if active_entry.user_id == target_user_id:
                        entry = active_entry
                        break
            if entry is not None and entry in self._active:
                self._active.remove(entry)
            waiting_count = len(self._waiting)
            active_count = len(self._active)
        if entry is None:
            logger.warning("[queue] release requested for non-active user=%s", target_user_id)
            return
        if elapsed_seconds > 0:
            self._avg_request_seconds = (
                1 - _ROLLING_AVG_WEIGHT
            ) * self._avg_request_seconds + _ROLLING_AVG_WEIGHT * elapsed_seconds
        self._semaphore.release()
        health.increment("llm_queue_released")
        log_event(
            "llm_queue_released",
            entry.run_id or "unknown",
            user_id=entry.user_id,
            task_kind=entry.task_kind,
            load_class=entry.load_class,
            elapsed_seconds=round(elapsed_seconds, 3),
            avg_request_seconds=round(self._avg_request_seconds, 3),
            waiting_count=waiting_count,
            active_count=active_count,
            max_concurrent=self._max_concurrent,
        )
        logger.debug(
            "[queue] user=%s released slot (elapsed=%.1fs, avg=%.1fs)",
            entry.user_id,
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
        return math.ceil((pos + 1) / self._max_concurrent) * self._avg_request_seconds

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
