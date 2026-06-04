from __future__ import annotations

import asyncio

from corpclaw_lite.channels.web.orchestrator import WebChannelOrchestrator
from corpclaw_lite.config.loader import load_settings
from corpclaw_lite.paths import PROJECT_ROOT

__all__ = [
    "run_web_channel",
]


async def run_web_channel() -> None:
    """Start the web channel and run until interrupted."""
    settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    orchestrator = WebChannelOrchestrator(settings)
    try:
        await orchestrator.start()
        await orchestrator.run_until_shutdown()
    except asyncio.CancelledError:
        pass
    finally:
        await orchestrator.stop()
