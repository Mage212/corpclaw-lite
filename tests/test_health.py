"""Tests for health metrics module."""

from __future__ import annotations

from corpclaw_lite.logging import health


def test_health_increment_and_get() -> None:
    """Verify increment updates counters and get_stats returns them."""
    # Reset counters for clean test
    health._counters.clear()

    health.increment("requests")
    health.increment("requests")
    health.increment("tool_calls", 5)
    health.increment("errors")

    stats = health.get_stats()
    assert stats["status"] == "ok"
    assert stats["requests"] == 2
    assert stats["tool_calls"] == 5
    assert stats["errors"] == 1
    assert "uptime_seconds" in stats
    assert isinstance(stats["uptime_seconds"], float)


def test_health_stats_default_zeros() -> None:
    """get_stats returns zero for unset counters."""
    health._counters.clear()
    stats = health.get_stats()
    assert stats["requests"] == 0
    assert stats["tool_calls"] == 0
    assert stats["errors"] == 0
    assert stats["status"] == "ok"
    assert stats["uptime_seconds"] > 0
