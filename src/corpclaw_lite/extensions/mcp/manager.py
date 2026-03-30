"""
MCPManager — loads MCP server configs from YAML and registers their tools.

Config file format (config/mcp_servers.yaml)::

    servers:
      - name: filesystem
        command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
      - name: fetch
        command: ["uvx", "mcp-server-fetch"]
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import yaml

from corpclaw_lite.extensions.mcp.adapter import MCPToolAdapter
from corpclaw_lite.extensions.mcp.client import MCPClient
from corpclaw_lite.extensions.tools.registry import ToolRegistry

__all__ = [
    "MCPManager",
]

logger = logging.getLogger(__name__)


class MCPManager:
    """
    Reads mcp_servers.yaml, connects to each MCP server, and registers
    all their tools into the provided ToolRegistry.

    Call ``await manager.connect_all(registry)`` once at startup.
    Call ``await manager.disconnect_all()`` on shutdown.
    """

    def __init__(self, config_path: str | Path = "config/mcp_servers.yaml") -> None:
        self._config_path = Path(config_path)
        self._clients: list[MCPClient] = []

    async def connect_all(self, registry: ToolRegistry) -> int:
        """
        Connect to all configured MCP servers and register their tools.
        Returns number of tools registered.
        """
        if not self._config_path.exists():
            logger.debug("No MCP config found at %s, skipping.", self._config_path)
            return 0

        with self._config_path.open(encoding="utf-8") as f:
            raw = cast(dict[str, Any], yaml.safe_load(f) or {})

        servers = cast(list[dict[str, Any]], raw.get("servers", []))
        total = 0

        for server_cfg in servers:
            name = str(server_cfg.get("name", "unknown"))
            command = cast(list[str], server_cfg.get("command", []))
            if not command:
                logger.warning("MCP server '%s' has no command, skipping.", name)
                continue

            client = MCPClient()
            try:
                await client.connect(command)
                tools = await client.list_tools()
                for tool_def in tools:
                    adapter = MCPToolAdapter(tool_def=tool_def, client=client)
                    try:
                        registry.register(adapter)
                        total += 1
                        logger.info("Registered MCP tool '%s' from '%s'", tool_def.name, name)
                    except ValueError:
                        logger.warning(
                            "MCP tool '%s' from '%s' conflicts with existing tool, skipping.",
                            tool_def.name,
                            name,
                        )
                self._clients.append(client)
            except Exception as e:
                logger.error("Failed to connect to MCP server '%s': %s", name, e)

        return total

    async def disconnect_all(self) -> None:
        """Disconnect all connected MCP servers."""
        for client in self._clients:
            await client.disconnect()
        self._clients.clear()
