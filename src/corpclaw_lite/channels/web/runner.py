from __future__ import annotations

import asyncio
import logging

from corpclaw_lite.channels.web.orchestrator import WebChannelOrchestrator
from corpclaw_lite.config.loader import load_settings
from corpclaw_lite.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

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
        # S3-10: bound shutdown so a hung MCP disconnect, container stop or
        # websocket close cannot hold the process past shutdown_timeout_seconds.
        try:
            await asyncio.wait_for(
                orchestrator.stop(), timeout=settings.agent.shutdown_timeout_seconds
            )
        except TimeoutError:
            logger.warning(
                "Web shutdown exceeded %.1fs; forcing exit.",
                settings.agent.shutdown_timeout_seconds,
            )
