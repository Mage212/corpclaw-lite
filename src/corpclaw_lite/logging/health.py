# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
from __future__ import annotations

import time
from collections import Counter
from typing import Any

__all__ = [
    "get_stats",
    "increment",
    "reset_stats",
    "run_health_server",
]

_start_time = time.time()
_counters: Counter[str] = Counter()


def increment(metric: str, value: int = 1) -> None:
    """Increment a named metric counter."""
    _counters[metric] += value


def reset_stats() -> None:
    """Reset all counters and restart uptime clock. For testing only."""
    global _start_time
    _start_time = time.time()
    _counters.clear()


def get_stats() -> dict[str, Any]:
    """Return current health stats as a dictionary."""
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "requests": _counters.get("requests", 0),
        "tool_calls": _counters.get("tool_calls", 0),
        "errors": _counters.get("errors", 0),
        "llm_calls": _counters.get("llm_calls", 0),
        "llm_stream_calls": _counters.get("llm_stream_calls", 0),
        "llm_stream_fallbacks": _counters.get("llm_stream_fallbacks", 0),
        "llm_stream_stalls": _counters.get("llm_stream_stalls", 0),
        "llm_reasoning_chars": _counters.get("llm_reasoning_chars", 0),
        "llm_content_chars": _counters.get("llm_content_chars", 0),
        "llm_timeouts": _counters.get("llm_timeouts", 0),
        "tool_errors": _counters.get("tool_errors", 0),
        "guard_blocks": _counters.get("guard_blocks", 0),
        "approval_denied": _counters.get("approval_denied", 0),
        "active_requests": _counters.get("active_requests", 0),
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
