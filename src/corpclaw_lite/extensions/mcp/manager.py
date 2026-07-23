# pyright: reportUnknownVariableType=false
"""
MCPManager — loads MCP server configs from YAML and registers their tools.

Config file format (config/mcp_servers.yaml)::

    servers:
      - name: filesystem
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
        env:
          ALLOWED_PATH: "/workspace"

      # Legacy single-list format also supported:
      - name: fetch
        command: ["uvx", "mcp-server-fetch"]
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, cast

import yaml

from corpclaw_lite.config.interpolation import interpolate_recursive
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

    Tracks which tools belong to which server so that MCPHotReloader can
    cleanly unregister them when a server is removed or changed.

    Call ``await manager.connect_all(registry)`` once at startup.
    Call ``await manager.disconnect_all()`` on shutdown.
    """

    def __init__(
        self, config_path: str | Path | list[str | Path] = "config/mcp_servers.yaml"
    ) -> None:
        """Create the manager.

        Args:
            config_path: One or more mcp_servers.yaml paths. When several are
                given, their ``servers:`` lists are merged in order (later paths
                override earlier ones by server ``name``). A single path keeps
                backward compatibility.
        """
        if isinstance(config_path, list):
            self._config_paths: list[Path] = [Path(p) for p in config_path]
        else:
            self._config_paths = [Path(config_path)]
        self._clients: dict[str, MCPClient] = {}  # server_name → client
        self._server_tools: dict[str, list[str]] = {}  # server_name → [tool_name, ...]

    @property
    def config_paths(self) -> list[Path]:
        """All config file paths this manager reads from (for watchers/tests)."""
        return list(self._config_paths)

    async def connect_all(self, registry: ToolRegistry) -> int:
        """
        Connect to all configured MCP servers and register their tools.
        Returns number of tools registered.
        """
        servers = self._load_config()
        total = 0
        for server_cfg in servers:
            count = await self._connect_server(server_cfg, registry)
            total += count
        return total

    async def disconnect_all(self) -> None:
        """Disconnect all connected MCP servers."""
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()
        self._server_tools.clear()

    async def disconnect_server(self, name: str, registry: ToolRegistry) -> None:
        """Disconnect a single server and unregister its tools from registry."""
        client = self._clients.pop(name, None)
        if client:
            await client.disconnect()
        for tool_name in self._server_tools.pop(name, []):
            registry.unregister(tool_name)
            logger.info("MCP: unregistered tool '%s' from server '%s'", tool_name, name)

    async def reconnect_server(self, server_cfg: dict[str, Any], registry: ToolRegistry) -> int:
        """Disconnect and reconnect a single server with new config."""
        name = str(server_cfg.get("name", "unknown"))
        await self.disconnect_server(name, registry)
        return await self._connect_server(server_cfg, registry)

    def get_server_names(self) -> list[str]:
        """Return names of all currently connected servers."""
        return list(self._clients.keys())

    def load_config_raw(self) -> list[dict[str, Any]]:
        """Return the current config file content (for hot-reload diffing)."""
        return self._load_config()

    def get_server_tools(self) -> dict[str, list[str]]:
        """Snapshot of ``{server_name: [tool_names]}`` for connected servers.

        Etap 4: powers the Extensions UI's MCP section.
        """
        return {name: list(tools) for name, tools in self._server_tools.items()}

    def get_server_status(self) -> dict[str, str]:
        """Return ``{server_name: "connected" | "disconnected"}``.

        Diff configured servers (from YAML) against connected ones (in ``_clients``).
        Etap 4: powers the Extensions UI's MCP status badges.
        """
        configured = {str(s.get("name", "unknown")) for s in self._load_config()}
        connected = set(self._clients.keys())
        return {name: ("connected" if name in connected else "disconnected") for name in configured}

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_config(self) -> list[dict[str, Any]]:
        """Read and interpolate all mcp_servers.yaml files, merge servers by name.

        Files are processed in order; a server ``name`` defined in a later file
        overrides an earlier definition (last wins). Missing files are skipped.
        Returns the merged ``servers`` list.
        """
        merged_by_name: dict[str, dict[str, Any]] = {}
        for path in self._config_paths:
            if not path.exists():
                logger.debug("No MCP config found at %s, skipping.", path)
                continue
            with path.open(encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            data = cast(dict[str, Any], interpolate_recursive(raw))
            servers = cast(list[dict[str, Any]], data.get("servers", []))
            for server in servers:
                name = str(server.get("name", "unknown"))
                merged_by_name[name] = server
        return list(merged_by_name.values())

    def _parse_command(self, server_cfg: dict[str, Any]) -> list[str]:
        """Support both formats:
        - command: "npx", args: ["-y", "..."]  (YAML-friendly)
        - command: ["npx", "-y", "..."]         (legacy list)
        """
        raw_command = server_cfg.get("command", [])
        if isinstance(raw_command, str):
            return [raw_command] + cast(list[str], server_cfg.get("args", []))
        return list(raw_command)

    async def _connect_server(self, server_cfg: dict[str, Any], registry: ToolRegistry) -> int:
        """Connect to a single server and register its tools. Returns tool count."""
        name = str(server_cfg.get("name", "unknown"))
        command = self._parse_command(server_cfg)
        env_vars = cast(dict[str, str], server_cfg.get("env", {}))

        if not command:
            logger.warning("MCP server '%s' has no command, skipping.", name)
            return 0

        client = MCPClient()
        try:
            await client.connect(command, env=env_vars if env_vars else None)
            tools = await client.list_tools()
            registered: list[str] = []
            for tool_def in tools:
                adapter = MCPToolAdapter(tool_def=tool_def, client=client, server_name=name)
                try:
                    registry.register(adapter, allow_replace=False)
                    registered.append(tool_def.name)
                    logger.info("MCP: registered tool '%s' from server '%s'", tool_def.name, name)
                except ValueError:
                    logger.warning(
                        "MCP tool '%s' from '%s' conflicts with existing tool, skipping.",
                        tool_def.name,
                        name,
                    )
            self._clients[name] = client
            self._server_tools[name] = registered
            logger.info("MCP: connected server '%s' (%d tools)", name, len(registered))
            return len(registered)
        except Exception as e:
            logger.error("MCP: failed to connect to server '%s': %s", name, e)
            with contextlib.suppress(Exception):
                await client.disconnect()
            return 0
