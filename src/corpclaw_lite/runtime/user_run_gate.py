"""Process-local per-user 1-in-flight gate (Sprint 2 / C3).

Shared by :class:`~corpclaw_lite.channels.service.AgentRequestService` (web /
headless / scheduler) and Telegram orchestrator so the same ``user.id`` cannot
run two agent workflows concurrently **in one process**.

Multi-process deploys (separate telegram + web processes) still have separate
gates — durable cross-process locking is a later follow-up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

__all__ = [
    "RunningRequestInfo",
    "UserRunGate",
    "get_user_run_gate",
    "reset_user_run_gate_for_tests",
]


@dataclass(frozen=True, slots=True)
class RunningRequestInfo:
    """In-flight workflow metadata for a single user."""

    session_id: int | None = None
    title: str | None = None


class UserRunGate:
    """Async mutex map: at most one active request per user id."""

    def __init__(self) -> None:
        self._active: dict[int, RunningRequestInfo] = {}
        self._lock = asyncio.Lock()

    async def try_start(
        self,
        user_id: int,
        *,
        session_id: int | None = None,
        title: str | None = None,
    ) -> bool:
        """Return False when the user already has an active workflow."""
        async with self._lock:
            if user_id in self._active:
                return False
            self._active[user_id] = RunningRequestInfo(session_id=session_id, title=title)
            return True

    async def finish(self, user_id: int) -> RunningRequestInfo | None:
        """Clear the in-flight flag; return previous info if any."""
        async with self._lock:
            return self._active.pop(user_id, None)

    async def update(
        self,
        user_id: int,
        *,
        session_id: int | None = None,
        title: str | None = None,
    ) -> bool:
        """Refresh metadata for an already-running user. Returns False if idle."""
        async with self._lock:
            if user_id not in self._active:
                return False
            self._active[user_id] = RunningRequestInfo(session_id=session_id, title=title)
            return True

    async def get(self, user_id: int) -> RunningRequestInfo | None:
        async with self._lock:
            return self._active.get(user_id)

    async def active_count(self) -> int:
        async with self._lock:
            return len(self._active)


_process_gate: UserRunGate | None = None


def get_user_run_gate() -> UserRunGate:
    """Return the process-wide gate (created once)."""
    global _process_gate
    if _process_gate is None:
        _process_gate = UserRunGate()
    return _process_gate


def reset_user_run_gate_for_tests() -> None:
    """Drop the process gate so tests get a fresh instance."""
    global _process_gate
    _process_gate = None
