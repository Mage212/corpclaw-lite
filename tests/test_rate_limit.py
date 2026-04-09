"""Tests for per-user sliding window rate limiter."""

from __future__ import annotations

import time

import pytest

from corpclaw_lite.channels.telegram.rate_limit import RateLimiter


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_under_limit_passes(self) -> None:
        """Requests within the limit should all pass."""
        limiter = RateLimiter(max_per_minute=10)
        for _ in range(9):
            assert await limiter.check(1001)

    @pytest.mark.asyncio
    async def test_at_limit_blocked(self) -> None:
        """The request that exceeds the limit should be blocked."""
        limiter = RateLimiter(max_per_minute=5)
        for _ in range(5):
            assert await limiter.check(2002)
        assert not await limiter.check(2002)

    @pytest.mark.asyncio
    async def test_different_users_independent(self) -> None:
        """Rate limits are per-user, not global."""
        limiter = RateLimiter(max_per_minute=3)
        for _ in range(3):
            assert await limiter.check(100)
        assert not await limiter.check(100)
        assert await limiter.check(200)

    @pytest.mark.asyncio
    async def test_window_slides(self) -> None:
        """After timestamps expire outside the 1-minute window, user can send again."""
        limiter = RateLimiter(max_per_minute=2)
        assert await limiter.check(300)
        assert await limiter.check(300)
        assert not await limiter.check(300)

        old_time = time.monotonic() - 120.0
        limiter._timestamps[300] = [old_time, old_time]

        assert await limiter.check(300)

    @pytest.mark.asyncio
    async def test_cleanup_removes_inactive(self) -> None:
        """Cleanup should remove users with empty timestamp lists."""
        limiter = RateLimiter(max_per_minute=10)

        await limiter.check(400)
        limiter._timestamps[400] = []
        limiter._timestamps[500] = []

        await limiter.cleanup()
        assert 400 not in limiter._timestamps
        assert 500 not in limiter._timestamps
