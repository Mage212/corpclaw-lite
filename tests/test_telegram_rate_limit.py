"""Tests for Telegram channel rate limiter."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from corpclaw_lite.channels.telegram.rate_limit import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_under_limit():
    limiter = RateLimiter(max_per_minute=2)

    assert await limiter.check(1) is True
    assert await limiter.check(1) is True
    assert await limiter.check(1) is False  # Hits max 2
    assert await limiter.check(2) is True  # Different user


@pytest.mark.asyncio
async def test_rate_limiter_window_reset():
    limiter = RateLimiter(max_per_minute=1)

    # First request works
    now = datetime.now()
    with patch("corpclaw_lite.channels.telegram.rate_limit.datetime") as mock_dt:
        mock_dt.now.return_value = now
        assert await limiter.check(1) is True

        # Immediate second fails
        assert await limiter.check(1) is False

        # Shift time by 61 seconds
        future = now + timedelta(seconds=61)
        mock_dt.now.return_value = future

        # Request works again
        assert await limiter.check(1) is True


@pytest.mark.asyncio
async def test_rate_limiter_cleanup():
    limiter = RateLimiter(max_per_minute=5)

    now = datetime.now()
    with patch("corpclaw_lite.channels.telegram.rate_limit.datetime") as mock_dt:
        mock_dt.now.return_value = now

        # Give user 1 a request
        await limiter.check(1)

        # Fast forward time to expire the request
        future = now + timedelta(seconds=61)
        mock_dt.now.return_value = future

        # Explicit check to trigger the rolling window cleanup internally
        await limiter.check(2)

        # User 1 still has an empty list
        assert 1 in limiter._timestamps

        # Run explicit cron cleanup
        await limiter.cleanup()

        # User 1 should be gone
        assert 1 not in limiter._timestamps
        # User 2 should remain
        assert 2 in limiter._timestamps
