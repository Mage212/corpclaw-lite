# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
from __future__ import annotations

import time
from collections import Counter
from typing import Any

__all__ = [
    "get_stats",
    "increment",
    "run_health_server",
]

_start_time = time.time()
_counters: Counter[str] = Counter()


def increment(metric: str, value: int = 1) -> None:
    """Increment a named metric counter."""
    _counters[metric] += value


def get_stats() -> dict[str, Any]:
    """Return current health stats as a dictionary."""
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "requests": _counters.get("requests", 0),
        "tool_calls": _counters.get("tool_calls", 0),
        "errors": _counters.get("errors", 0),
    }


async def run_health_server(host: str = "0.0.0.0", port: int = 8080) -> Any:
    """Start a minimal aiohttp server exposing GET /health.

    Returns the ``AppRunner`` so callers can call ``runner.cleanup()`` on shutdown.
    """
    try:
        from aiohttp import web  # type: ignore[import-untyped]
    except ImportError as e:
        raise ImportError("aiohttp is required for the health endpoint. Run: uv add aiohttp") from e

    async def health_handler(request: web.Request) -> web.Response:
        return web.json_response(get_stats())

    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
