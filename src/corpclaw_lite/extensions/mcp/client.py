"""
Stdio-based MCP (Model Context Protocol) client.

Launches an MCP server as a subprocess and communicates via JSON-RPC over stdio.
Implements the minimal subset needed for tool listing and invocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, cast

__all__ = [
    "MCPClient",
    "MCPClientError",
    "MCPToolDef",
]

logger = logging.getLogger(__name__)

_JSONRPC = "2.0"


@dataclass
class MCPToolDef:
    """Definition of a tool exposed by an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {})


class MCPClientError(Exception):
    """Raised for errors communicating with an MCP server."""


class MCPClient:
    """
    Minimal stdio MCP client.

    Usage::

        client = MCPClient()
        await client.connect(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
        tools = await client.list_tools()
        result = await client.call_tool("read_file", {"path": "/tmp/test.txt"})
        await client.disconnect()
    """

    def __init__(self, timeout: float = 30.0, total_timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._total_timeout = total_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def connect(self, command: list[str], env: dict[str, str] | None = None) -> None:
        """Launch the MCP server subprocess and perform the initialization handshake.

        Args:
            command: The server executable and its arguments.
            env: Extra environment variables to pass to the server process.
                 These are merged with the current process environment.
        """
        proc_env: dict[str, str] | None = None
        if env:
            proc_env = {**os.environ, **env}

        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=proc_env,
        )
        logger.info("MCP server started: %s", " ".join(command))

        # Initialize handshake
        await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "corpclaw-lite", "version": "0.1.0"},
            },
        )
        # Send initialized notification (no response expected)
        await self._send_notification("notifications/initialized", {})

    async def list_tools(self) -> list[MCPToolDef]:
        """Return list of tools provided by the connected MCP server."""
        result = await self._send_request("tools/list", {})
        tools_raw = result.get("tools", [])
        tools: list[MCPToolDef] = []
        for t in tools_raw:
            tools.append(
                MCPToolDef(
                    name=str(t.get("name", "")),
                    description=str(t.get("description", "")),
                    input_schema=dict(t.get("inputSchema", {})),
                )
            )
        return tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        """Invoke a tool on the MCP server and return its text result."""
        result = await self._send_request("tools/call", {"name": name, "arguments": args})
        # MCP returns content as a list of content blocks
        content = cast(list[Any], result.get("content", []))
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                typed_block = cast(dict[str, Any], block)
                if typed_block.get("type") == "text":
                    parts.append(str(typed_block.get("text", "")))
        return "\n".join(parts) if parts else str(result)

    async def disconnect(self) -> None:
        """Terminate the MCP server subprocess.

        Sends SIGTERM first, then escalates to SIGKILL if the process
        doesn't exit within 5 seconds — prevents ghost MCP processes.
        """
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning("MCP server did not terminate in 5s, sending SIGKILL...")
                try:
                    self._process.kill()
                    await self._process.wait()
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Error terminating MCP server: %s", e)
                # Last resort — ensure no ghost process
                with contextlib.suppress(Exception):
                    self._process.kill()
            self._process = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        msg = {"jsonrpc": _JSONRPC, "method": method, "params": params}
        await self._write(msg)

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and return the result dict."""
        try:
            return await asyncio.wait_for(
                self._send_request_inner(method, params),
                timeout=self._total_timeout,
            )
        except TimeoutError as e:
            raise MCPClientError(
                f"MCP request '{method}' exceeded total timeout of {self._total_timeout}s"
            ) from e

    async def _send_request_inner(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Inner implementation with per-line timeout."""
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise MCPClientError("MCP client is not connected.")

        req_id = self._next_id()
        msg = {"jsonrpc": _JSONRPC, "id": req_id, "method": method, "params": params}
        await self._write(msg)

        # Read response lines until we find the matching id
        try:
            while True:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(), timeout=self._timeout
                )
                if not line:
                    raise MCPClientError("MCP server closed connection unexpectedly.")
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                response = json.loads(text)
                if response.get("id") != req_id:
                    continue  # notification or different request

                if "error" in response:
                    err = response["error"]
                    raise MCPClientError(f"MCP error {err.get('code')}: {err.get('message')}")

                return dict(response.get("result", {}))
        except TimeoutError as e:
            raise MCPClientError(f"MCP server did not respond within {self._timeout}s") from e

    async def _write(self, msg: dict[str, Any]) -> None:
        """Serialize msg as JSON and write to server stdin."""
        if not self._process or not self._process.stdin:
            raise MCPClientError("MCP client is not connected.")
        data = json.dumps(msg).encode("utf-8") + b"\n"
        self._process.stdin.write(data)
        await self._process.stdin.drain()
