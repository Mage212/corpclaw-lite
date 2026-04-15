"""PluginToolProxy — delegates tool execution to an isolated subprocess.

Flow::

    PluginLoader.load_plugin()
      → sync subprocess --introspect → schema JSON
      → PluginToolProxy(schema, tool_path)

    PluginToolProxy.execute(**kwargs)
      → lazily spawn async subprocess (sandbox_worker.py)
      → JSON-RPC stdin/stdout
      → return result string

    PluginToolProxy.kill()
      → terminate subprocess
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

__all__ = [
    "PluginToolProxy",
]

logger = logging.getLogger(__name__)

_SANDBOX_WORKER_PATH = Path(__file__).parent / "sandbox_worker.py"
_EXECUTE_TIMEOUT = 30.0


def introspect_tool(tool_path: Path) -> dict[str, Any] | None:
    """Run sandbox_worker in introspect mode and return tool schema."""
    try:
        result = subprocess.run(
            [sys.executable, str(_SANDBOX_WORKER_PATH), str(tool_path), "--introspect"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error("Plugin tool introspection failed for %s: %s", tool_path, e)
        return None

    if result.returncode != 0:
        logger.error(
            "Plugin tool introspection failed for %s: %s", tool_path, result.stderr.strip()
        )
        return None

    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        logger.error("Invalid introspection output from %s: %s", tool_path, result.stdout[:200])
        return None


class PluginToolProxy(Tool):
    """Proxy tool that delegates execution to a sandboxed subprocess.

    The subprocess is lazily started on first execute() call and reused
    for subsequent calls. An asyncio.Lock serializes concurrent requests
    through the single subprocess stdin/stdout channel.
    """

    name: str
    description: str
    params: list[ToolParam]
    risk_level: RiskLevel
    parallel_safe: bool
    terminal: bool

    def __init__(
        self,
        name: str,
        description: str,
        params: list[ToolParam],
        risk_level: RiskLevel,
        tool_path: Path,
        parallel_safe: bool = True,
        terminal: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.params = params
        self.risk_level = risk_level
        self.parallel_safe = parallel_safe
        self.terminal = terminal
        self._tool_path = tool_path
        self._process: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._lock = asyncio.Lock()

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process

        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_SANDBOX_WORKER_PATH),
            str(self._tool_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        logger.debug(
            "PluginToolProxy: spawned subprocess for %s (pid=%d)", self.name, self._process.pid
        )
        return self._process

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def execute(self, **kwargs: Any) -> str:
        async with self._lock:
            try:
                proc = await self._ensure_process()
            except Exception as e:
                return f"Error: failed to start plugin subprocess for {self.name}: {e}"

            request = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "execute",
                    "params": {"kwargs": kwargs},
                }
            )

            try:
                if proc.stdin is None:
                    return f"Error: plugin subprocess for {self.name} has no stdin"
                proc.stdin.write(request.encode() + b"\n")
                await proc.stdin.drain()

                if proc.stdout is None:
                    return f"Error: plugin subprocess for {self.name} has no stdout"
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=_EXECUTE_TIMEOUT)

                if not line:
                    self._process = None
                    return f"Error: plugin subprocess for {self.name} closed unexpectedly"

                response = json.loads(line.decode())
                if "error" in response:
                    return f"Error: {response['error'].get('message', 'unknown error')}"
                return str(response.get("result", ""))
            except TimeoutError:
                await self.kill()
                return f"Error: plugin tool '{self.name}' execution timed out"
            except Exception as e:
                self._process = None
                return f"Error: plugin subprocess for {self.name} communication failed: {e}"

    async def kill(self) -> None:
        """Terminate the subprocess if running and wait for exit."""
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            await self._process.wait()
            logger.debug(
                "PluginToolProxy: killed subprocess for %s (pid=%d)", self.name, self._process.pid
            )
            self._process = None
