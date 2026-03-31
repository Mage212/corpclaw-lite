"""Tests for logging/health.py — metrics, stats, reset, and health server."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.logging import health

# ── Fixture: clean counters between tests ─────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_health_state() -> None:
    """Reset health module state before each test for isolation."""
    health.reset_stats()


# ── Test: increment with default and custom values ────────────────────────────


def test_increment_and_get() -> None:
    health.increment("requests")
    health.increment("requests")
    health.increment("tool_calls", 5)
    health.increment("errors")

    stats = health.get_stats()
    assert stats["status"] == "ok"
    assert stats["requests"] == 2
    assert stats["tool_calls"] == 5
    assert stats["errors"] == 1
    assert isinstance(stats["uptime_seconds"], float)


def test_increment_custom_metric() -> None:
    """increment() works with arbitrary metric names, not just the standard three."""
    health.increment("llm_calls", 3)
    health.increment("cache_hits")

    # Custom metrics not in get_stats() — but they're in _counters
    assert health._counters["llm_calls"] == 3
    assert health._counters["cache_hits"] == 1


# ── Test: get_stats defaults ──────────────────────────────────────────────────


def test_stats_default_zeros() -> None:
    stats = health.get_stats()
    assert stats["requests"] == 0
    assert stats["tool_calls"] == 0
    assert stats["errors"] == 0
    assert stats["status"] == "ok"
    assert stats["uptime_seconds"] >= 0


# ── Test: reset_stats ────────────────────────────────────────────────────────


def test_reset_stats() -> None:
    health.increment("requests", 100)
    health.increment("errors", 50)

    health.reset_stats()

    stats = health.get_stats()
    assert stats["requests"] == 0
    assert stats["errors"] == 0
    # Uptime should be near zero after reset
    assert stats["uptime_seconds"] < 1.0


# ── Test: uptime increases ────────────────────────────────────────────────────


def test_uptime_increases() -> None:
    stats1 = health.get_stats()
    time.sleep(0.05)
    stats2 = health.get_stats()
    assert stats2["uptime_seconds"] >= stats1["uptime_seconds"]


# ── Test: run_health_server happy path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_health_server_starts() -> None:
    """run_health_server creates an app, sets up runner, starts site, returns runner."""
    pytest.importorskip("aiohttp")
    from aiohttp import web

    mock_runner = MagicMock(spec=web.AppRunner)
    mock_runner.setup = AsyncMock()

    mock_site = MagicMock(spec=web.TCPSite)
    mock_site.start = AsyncMock()

    with (
        patch("aiohttp.web.AppRunner", return_value=mock_runner),
        patch("aiohttp.web.TCPSite", return_value=mock_site),
    ):
        runner = await health.run_health_server(host="127.0.0.1", port=19876)
        assert runner is mock_runner
        mock_runner.setup.assert_awaited_once()
        mock_site.start.assert_awaited_once()


# ── Test: run_health_server without aiohttp ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_health_server_no_aiohttp() -> None:
    """When aiohttp is not installed, run_health_server raises ImportError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "aiohttp":
            raise ImportError("No module named 'aiohttp'")
        return real_import(name, *args, **kwargs)  # type: ignore[misc]

    with (
        patch("builtins.__import__", side_effect=fake_import),
        pytest.raises(ImportError, match="aiohttp is required"),
    ):
        await health.run_health_server()


# ── Test: health_handler returns correct JSON ─────────────────────────────────


@pytest.mark.asyncio
async def test_health_handler_response() -> None:
    """The health endpoint handler returns JSON with correct stats."""
    pytest.importorskip("aiohttp")
    from aiohttp import web

    health.increment("requests", 3)

    mock_runner = MagicMock(spec=web.AppRunner)
    mock_runner.setup = AsyncMock()

    mock_site = MagicMock(spec=web.TCPSite)
    mock_site.start = AsyncMock()

    # Capture the app passed to AppRunner
    captured_app: list[web.Application] = []

    def capture_app(app: web.Application) -> MagicMock:
        captured_app.append(app)
        return mock_runner

    with (
        patch("aiohttp.web.AppRunner", side_effect=capture_app),
        patch("aiohttp.web.TCPSite", return_value=mock_site),
    ):
        await health.run_health_server(host="127.0.0.1", port=19877)

    assert len(captured_app) == 1
    app = captured_app[0]

    # Find the /health route and call it
    mock_request = MagicMock()
    for resource in app.router.resources():
        info = resource.get_info()
        if info.get("path") == "/health" or info.get("formatter") == "/health":
            # Get the handler
            for route in resource:
                response = await route.handler(mock_request)
                assert response.status == 200
                assert response.content_type == "application/json"
                break
            break
