"""Tests for Telegram channel rate limiter."""

from unittest.mock import patch

import pytest

from corpclaw_lite.channels.telegram.rate_limit import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_under_limit():
    limiter = RateLimiter(max_per_minute=2)

    assert await limiter.check(1) is True
    assert await limiter.check(1) is True
    assert await limiter.check(1) is False
    assert await limiter.check(2) is True


@pytest.mark.asyncio
async def test_rate_limiter_window_reset():
    limiter = RateLimiter(max_per_minute=1)

    base_time = 1000.0
    with patch("corpclaw_lite.channels.telegram.rate_limit.time.monotonic", return_value=base_time):
        assert await limiter.check(1) is True
        assert await limiter.check(1) is False

        with patch(
            "corpclaw_lite.channels.telegram.rate_limit.time.monotonic",
            return_value=base_time + 61.0,
        ):
            assert await limiter.check(1) is True


@pytest.mark.asyncio
async def test_rate_limiter_cleanup():
    limiter = RateLimiter(max_per_minute=5)

    base_time = 1000.0
    with patch("corpclaw_lite.channels.telegram.rate_limit.time.monotonic", return_value=base_time):
        await limiter.check(1)

        with patch(
            "corpclaw_lite.channels.telegram.rate_limit.time.monotonic",
            return_value=base_time + 61.0,
        ):
            await limiter.check(2)

            assert 1 in limiter._timestamps

            await limiter.cleanup()

            assert 1 not in limiter._timestamps
            assert 2 in limiter._timestamps
