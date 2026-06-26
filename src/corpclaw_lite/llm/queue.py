"""LLM request queue — concurrency-limited execution with position tracking.

Bounds the number of concurrent LLM inference requests to match GPU capacity.
Tracks queue positions so callers can notify users about wait times.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from corpclaw_lite.logging import health
from corpclaw_lite.logging.trace import log_event

__all__ = [
    "LLMLoadClass",
    "LLMQueueStatus",
    "LLMRequestQueue",
    "QueueEntry",
    "SlotAffinityConfig",
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
QueueStrategy = Literal["simple", "slot_affinity"]
SlotKind = Literal["simple", "sticky", "overflow"]


@dataclass(frozen=True)
class SlotAffinityConfig:
    """Configuration for llama.cpp-compatible slot affinity."""

    enabled: bool = False
    provider_names: tuple[str, ...] = ("llamacpp",)
    sticky_slot_ids: tuple[int, ...] = (0, 1, 2)
    overflow_slot_ids: tuple[int, ...] = (3,)
    idle_ttl_seconds: float = 120.0
    cache_prompt: bool = True
    auxiliary_policy: Literal["overflow_only"] = "overflow_only"


@dataclass
class _SlotState:
    """Runtime state for one logical inference slot."""

    slot_id: int
    kind: Literal["sticky", "overflow"]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    assigned_user_id: str | None = None
    active: bool = False
    expires_at: float = 0.0


@dataclass
class LLMQueueStatus:
    """User-facing snapshot of a request waiting for an LLM inference slot."""

    user_id: str
    task_kind: str
    load_class: LLMLoadClass
    position: int | None
    estimated_wait_seconds: float | None
    waiting_count: int
    active_count: int
    max_concurrent: int
    wait_seconds: float


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
    provider_name: str | None = None
    slot_id: int | None = None
    slot_kind: SlotKind = "simple"
    assignment_reused: bool = False
    backend_extra_body: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

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

    def __init__(
        self,
        max_concurrent: int = 2,
        *,
        strategy: QueueStrategy = "simple",
        slot_affinity: SlotAffinityConfig | None = None,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._waiting: list[QueueEntry] = []
        self._active: list[QueueEntry] = []
        self._avg_request_seconds: float = _DEFAULT_AVG_SECONDS
        self._lock = asyncio.Lock()
        self._max_concurrent = max_concurrent
        self._strategy: QueueStrategy = strategy
        self._slot_affinity = slot_affinity or SlotAffinityConfig()
        self._slots: dict[int, _SlotState] = {}
        self._slot_affinity_provider_warnings: set[str] = set()
        if self._strategy == "slot_affinity" and self._slot_affinity.enabled:
            for slot_id in self._slot_affinity.sticky_slot_ids:
                self._slots[slot_id] = _SlotState(slot_id=slot_id, kind="sticky")
            for slot_id in self._slot_affinity.overflow_slot_ids:
                self._slots[slot_id] = _SlotState(slot_id=slot_id, kind="overflow")
            if not self._slots:
                logger.warning("LLMRequestQueue slot_affinity enabled without slot ids")
                self._strategy = "simple"
        logger.info(
            "LLMRequestQueue initialized: max_concurrent=%d strategy=%s",
            max_concurrent,
            self._strategy,
        )

    async def acquire(
        self,
        user_id: str,
        *,
        task_kind: str = "default",
        load_class: LLMLoadClass = "interactive",
        run_id: str | None = None,
        provider_name: str | None = None,
        on_status: Callable[[LLMQueueStatus], None] | None = None,
        notify_position: bool = True,
        notify_interval_seconds: float = 30.0,
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
            provider_name=provider_name,
        )
        semaphore_acquired = False
        slot_lock_acquired = False
        notify_task: asyncio.Task[None] | None = None
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
        should_notify_waiting = pos > 0 or active_count >= self._max_concurrent
        if on_status is not None and should_notify_waiting:
            self._emit_queue_status(entry, on_status, include_position=notify_position)
            if notify_position and notify_interval_seconds > 0:
                notify_task = asyncio.create_task(
                    self._notify_waiting_status_loop(
                        entry,
                        on_status,
                        notify_interval_seconds,
                    )
                )
        selected_slot: _SlotState | None = None
        try:
            selected_slot = await self._select_slot(entry)
            if selected_slot is not None:
                await selected_slot.lock.acquire()
                slot_lock_acquired = True
            await self._semaphore.acquire()
            semaphore_acquired = True
            async with self._lock:
                if entry in self._waiting:
                    self._waiting.remove(entry)
                entry.acquired_at = time.monotonic()
                if selected_slot is not None:
                    selected_slot.active = True
                    entry.slot_id = selected_slot.slot_id
                    entry.slot_kind = selected_slot.kind
                    if selected_slot.kind == "sticky":
                        selected_slot.assigned_user_id = entry.user_id
                        selected_slot.expires_at = 0.0
                    entry.backend_extra_body = {
                        "id_slot": selected_slot.slot_id,
                        "cache_prompt": self._slot_affinity.cache_prompt,
                    }
                self._active.append(entry)
                waiting_count = len(self._waiting)
                active_count = len(self._active)
        except asyncio.CancelledError:
            if notify_task is not None:
                await self._cancel_notify_task(notify_task)
            async with self._lock:
                if entry in self._waiting:
                    self._waiting.remove(entry)
                if (
                    selected_slot is not None
                    and selected_slot.kind == "sticky"
                    and selected_slot.assigned_user_id == entry.user_id
                    and not selected_slot.active
                    and not entry.assignment_reused
                ):
                    selected_slot.assigned_user_id = None
                    selected_slot.expires_at = 0.0
                waiting_count = len(self._waiting)
                active_count = len(self._active)
            if semaphore_acquired:
                self._semaphore.release()
                if slot_lock_acquired and selected_slot is not None and selected_slot.lock.locked():
                    selected_slot.lock.release()
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
        if notify_task is not None:
            await self._cancel_notify_task(notify_task)
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
            provider_name=provider_name,
            slot_id=entry.slot_id,
            slot_kind=entry.slot_kind,
            assignment_reused=entry.assignment_reused,
            provider_extra_body_keys=sorted(entry.backend_extra_body.keys()),
        )
        if entry.slot_id is not None:
            health.increment("llm_slot_acquired")
            event_name = (
                "llm_slot_reused"
                if entry.assignment_reused
                else "llm_slot_overflow_used"
                if entry.slot_kind == "overflow"
                else "llm_slot_assigned"
            )
            log_event(
                event_name,
                run_id or "unknown",
                user_id=user_id,
                task_kind=task_kind,
                load_class=load_class,
                provider_name=provider_name,
                slot_id=entry.slot_id,
                slot_kind=entry.slot_kind,
                assignment_reused=entry.assignment_reused,
                queue_wait_ms=round(queue_wait * 1000, 1),
                idle_ttl_seconds=self._slot_affinity.idle_ttl_seconds,
                provider_extra_body_keys=sorted(entry.backend_extra_body.keys()),
            )
        logger.debug(
            "[queue] user=%s acquired slot after %.1fs (%s/%s)",
            user_id,
            queue_wait,
            task_kind,
            load_class,
        )
        return entry

    async def _notify_waiting_status_loop(
        self,
        entry: QueueEntry,
        callback: Callable[[LLMQueueStatus], None],
        interval_seconds: float,
    ) -> None:
        """Emit queue status periodically while an entry waits for a slot."""
        while entry.acquired_at is None:
            await asyncio.sleep(interval_seconds)
            if entry.acquired_at is None:
                self._emit_queue_status(entry, callback, include_position=True)

    @staticmethod
    async def _cancel_notify_task(task: asyncio.Task[None]) -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _queue_status_snapshot(
        self,
        entry: QueueEntry,
        *,
        include_position: bool,
    ) -> LLMQueueStatus:
        position = self.get_position(entry.user_id) if include_position else None
        estimated_wait = self.get_estimated_wait(entry.user_id) if include_position else None
        return LLMQueueStatus(
            user_id=entry.user_id,
            task_kind=entry.task_kind,
            load_class=entry.load_class,
            position=position,
            estimated_wait_seconds=estimated_wait,
            waiting_count=self.queue_length,
            active_count=self.active_count,
            max_concurrent=self._max_concurrent,
            wait_seconds=max(0.0, time.monotonic() - entry.enqueued_at),
        )

    def _emit_queue_status(
        self,
        entry: QueueEntry,
        callback: Callable[[LLMQueueStatus], None],
        *,
        include_position: bool,
    ) -> None:
        try:
            callback(self._queue_status_snapshot(entry, include_position=include_position))
        except Exception as e:
            logger.debug("[queue] status callback failed for user=%s: %s", entry.user_id, e)

    async def _select_slot(self, entry: QueueEntry) -> _SlotState | None:
        """Select a slot for the entry, or None when simple queue mode applies."""
        if not self._slot_affinity_applies(entry):
            return None
        async with self._lock:
            self._expire_idle_slots_locked(time.monotonic())
            if self._is_sticky_eligible(entry):
                existing = self._assigned_sticky_slot_locked(entry.user_id)
                if existing is not None:
                    entry.assignment_reused = True
                    return existing
                free = self._free_sticky_slot_locked()
                if free is not None:
                    free.assigned_user_id = entry.user_id
                    free.expires_at = 0.0
                    return free
            return self._overflow_slot_locked()

    def _slot_affinity_applies(self, entry: QueueEntry) -> bool:
        if self._strategy != "slot_affinity" or not self._slot_affinity.enabled:
            return False
        if not self._slots:
            return False
        if entry.provider_name is None:
            return False
        if entry.provider_name in self._slot_affinity.provider_names:
            return True
        if entry.provider_name not in self._slot_affinity_provider_warnings:
            self._slot_affinity_provider_warnings.add(entry.provider_name)
            logger.warning(
                "LLM slot_affinity is enabled but provider '%s' is not in provider_names=%s; "
                "using simple queue without id_slot/cache_prompt for this provider.",
                entry.provider_name,
                list(self._slot_affinity.provider_names),
            )
        return False

    @staticmethod
    def _is_sticky_eligible(entry: QueueEntry) -> bool:
        return entry.load_class == "interactive" and entry.task_kind == "default"

    def _assigned_sticky_slot_locked(self, user_id: str) -> _SlotState | None:
        for slot in self._slots.values():
            if slot.kind == "sticky" and slot.assigned_user_id == user_id:
                return slot
        return None

    def _free_sticky_slot_locked(self) -> _SlotState | None:
        for slot in self._slots.values():
            if slot.kind == "sticky" and slot.assigned_user_id is None and not slot.active:
                return slot
        return None

    def _overflow_slot_locked(self) -> _SlotState | None:
        for slot in self._slots.values():
            if slot.kind == "overflow":
                return slot
        return None

    def _expire_idle_slots_locked(self, now: float) -> None:
        for slot in self._slots.values():
            if (
                slot.kind == "sticky"
                and not slot.active
                and slot.assigned_user_id is not None
                and slot.expires_at > 0
                and now >= slot.expires_at
            ):
                expired_user_id = slot.assigned_user_id
                slot.assigned_user_id = None
                slot.expires_at = 0.0
                health.increment("llm_slot_expired")
                log_event(
                    "llm_slot_expired",
                    "unknown",
                    user_id=expired_user_id,
                    slot_id=slot.slot_id,
                    slot_kind=slot.kind,
                )

    async def release(self, entry: QueueEntry, elapsed_seconds: float) -> None:
        """Release an inference slot after the LLM call completes.

        Updates the rolling average request duration used for wait estimates.

        ``entry`` must be the ``QueueEntry`` returned by ``acquire()`` — the
        queue never resolves releases by bare user id anymore, so a caller with
        multiple concurrent entries cannot accidentally release the wrong one
        (the prior str-tolerant path silently dropped all-but-one entry and could
        leak the semaphore).
        """
        slot_to_release: _SlotState | None = None
        async with self._lock:
            if entry in self._active:
                self._active.remove(entry)
            if entry.slot_id is not None:
                slot_to_release = self._slots.get(entry.slot_id)
                if slot_to_release is not None:
                    slot_to_release.active = False
                    if slot_to_release.kind == "sticky":
                        slot_to_release.expires_at = (
                            time.monotonic() + self._slot_affinity.idle_ttl_seconds
                        )
                    else:
                        slot_to_release.assigned_user_id = None
                        slot_to_release.expires_at = 0.0
                    if slot_to_release.lock.locked():
                        slot_to_release.lock.release()
            waiting_count = len(self._waiting)
            active_count = len(self._active)
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
            provider_name=entry.provider_name,
            slot_id=entry.slot_id,
            slot_kind=entry.slot_kind,
            expires_at=slot_to_release.expires_at if slot_to_release is not None else None,
        )
        if entry.slot_id is not None:
            log_event(
                "llm_slot_released",
                entry.run_id or "unknown",
                user_id=entry.user_id,
                task_kind=entry.task_kind,
                load_class=entry.load_class,
                provider_name=entry.provider_name,
                slot_id=entry.slot_id,
                slot_kind=entry.slot_kind,
                expires_at=slot_to_release.expires_at if slot_to_release is not None else None,
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
