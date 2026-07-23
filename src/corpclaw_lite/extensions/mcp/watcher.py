"""
MCPHotReloader — watches mcp_servers.yaml for changes and reconnects servers.

Polls the config file mtime every ``poll_interval`` seconds (default: 10).
On change, computes a diff (added / removed / changed servers) and performs
targeted connect/disconnect without restarting the entire process.

Usage (mirrors SkillHotReloader)::

    reloader = MCPHotReloader(config_path, manager, registry)
    reloader.start()   # background asyncio task
    # ... run bot ...
    reloader.stop()
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from corpclaw_lite.extensions.mcp.manager import MCPManager
from corpclaw_lite.extensions.tools.registry import ToolRegistry

__all__ = [
    "MCPHotReloader",
]

logger = logging.getLogger(__name__)


class MCPHotReloader:
    """
    File-mtime watcher for config/mcp_servers.yaml.

    When the file changes:
    - New servers → connect + register tools
    - Removed servers → unregister tools + disconnect
    - Changed servers (command/args/env modified) → disconnect old + reconnect
    """

    def __init__(
        self,
        config_path: Path | str | list[str | Path],
        manager: MCPManager,
        registry: ToolRegistry,
        poll_interval: float = 10.0,
    ) -> None:
        if isinstance(config_path, list):
            self._paths: list[Path] = [Path(p) for p in config_path]
        else:
            self._paths = [Path(config_path)]
        self._manager = manager
        self._registry = registry
        self._poll_interval = poll_interval
        self._last_mtime: float | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            logger.info(
                "MCPHotReloader started watching: %s", ", ".join(str(p) for p in self._paths)
            )

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("MCPHotReloader stopped.")

    async def reload_now(self) -> None:
        """Trigger an immediate config recheck (Etap 4: manual reload button)."""
        await self._check()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll config file mtime; reload on change."""
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._check()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("MCPHotReloader error: %s", e)

    async def _check(self) -> None:
        """Check if any watched config file changed; reload if so.

        Tracks the maximum mtime across all config files, so an edit to any
        overlay file triggers a reload.
        """
        current_mtime = 0.0
        any_exists = False
        for path in self._paths:
            if path.exists():
                any_exists = True
                current_mtime = max(current_mtime, path.stat().st_mtime)
        if not any_exists:
            return
        if self._last_mtime is None:
            # First check after start — record mtime, no reload needed
            self._last_mtime = current_mtime
            return
        if current_mtime <= self._last_mtime:
            return

        logger.info("MCPHotReloader: config changed, reloading MCP servers...")
        self._last_mtime = current_mtime
        await self._reload()

    async def _reload(self) -> None:
        """Diff old vs new config and apply minimal changes."""
        new_servers: list[dict[str, Any]] = self._manager.load_config_raw()
        new_by_name: dict[str, dict[str, Any]] = {
            str(s.get("name", "unknown")): s for s in new_servers
        }
        current_names = set(self._manager.get_server_names())
        new_names = set(new_by_name.keys())

        # Servers to remove
        for name in current_names - new_names:
            logger.info("MCPHotReloader: removing server '%s'", name)
            await self._manager.disconnect_server(name, self._registry)

        # Servers to add
        for name in new_names - current_names:
            logger.info("MCPHotReloader: adding server '%s'", name)
            count = await self._manager.reconnect_server(new_by_name[name], self._registry)
            logger.info("MCPHotReloader: server '%s' connected, %d tools registered", name, count)

        # Servers in both — reconnect to pick up command/env changes
        for name in current_names & new_names:
            logger.info("MCPHotReloader: reconnecting server '%s'", name)
            count = await self._manager.reconnect_server(new_by_name[name], self._registry)
            logger.info("MCPHotReloader: server '%s' reconnected, %d tools registered", name, count)
