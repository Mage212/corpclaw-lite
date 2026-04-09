"""Per-user sliding window rate limiter for Telegram channel."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

__all__ = [
    "RateLimiter",
]

logger = logging.getLogger(__name__)


class RateLimiter:
    """Per-user sliding window rate limiter.

    Tracks message timestamps per Telegram user and blocks requests
    that exceed ``max_per_minute`` within a rolling 60-second window.
    """

    def __init__(self, max_per_minute: int = 10) -> None:
        self._max = max_per_minute
        self._timestamps: dict[int, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check(self, user_id: int) -> bool:
        """Return True if under limit, False if rate-limited."""
        async with self._lock:
            now = time.monotonic()
            cutoff = now - 60.0

            self._timestamps[user_id] = [ts for ts in self._timestamps[user_id] if ts > cutoff]

            if len(self._timestamps[user_id]) >= self._max:
                return False

            self._timestamps[user_id].append(now)
            return True

    async def cleanup(self) -> None:
        """Remove inactive users from the timestamps dict.

        Should be called periodically from a background task.
        """
        async with self._lock:
            now = time.monotonic()
            cutoff = now - 60.0

            inactive: list[int] = []
            for uid, ts_list in self._timestamps.items():
                active_ts = [ts for ts in ts_list if ts > cutoff]
                if not active_ts:
                    inactive.append(uid)
                else:
                    self._timestamps[uid] = active_ts

            for uid in inactive:
                del self._timestamps[uid]
            if inactive:
                logger.debug("Cleaned up %d inactive rate limit entries", len(inactive))
