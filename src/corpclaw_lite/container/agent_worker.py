"""Container-side agent worker — executes tools inside Docker containers.

Reads a signed IPC request from stdin, executes the requested tool,
signs the response, and writes it to stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING

from corpclaw_lite.security.ipc_auth import IPCAuth

if TYPE_CHECKING:
    from corpclaw_lite.extensions.tools.registry import ToolRegistry

__all__ = ["process_request"]


def _init_logging() -> None:
    logging.basicConfig(level=logging.ERROR)


def _build_container_registry() -> ToolRegistry:
    """Build a ToolRegistry with tools available inside containers.

    Lazy-loading tools here to speed up python startup in the container.
    """
    try:
        from corpclaw_lite.extensions.tools.builtin.excel import NormalizeExcelTool
        from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool
        from corpclaw_lite.extensions.tools.builtin.files import (
            EditFileTool,
            ListFilesTool,
            ReadFileTool,
            SearchFilesTool,
            WriteFileTool,
        )
        from corpclaw_lite.extensions.tools.registry import ToolRegistry
    except ImportError as e:
        print(f"Container tool import failed: {e}", file=sys.stderr)
        raise

    registry = ToolRegistry()
    for tool in [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
        ExecScriptTool(),
        NormalizeExcelTool(),
    ]:
        registry.register(tool)
    return registry


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = _build_container_registry()
    return _registry


def process_request() -> None:
    """Read from stdin, verify, execute tool, sign response, print to stdout."""
    _init_logging()
    # sys.stdin.read() hangs indefinitely on Docker Desktop for Mac because of EOF handling issues.
    # Since payload is sent as a single line JSON string, we use readline().
    input_data = sys.stdin.readline().strip()
    if not input_data:
        return

    response_payload: dict[str, str] = {"status": "error", "error": "Unknown error"}
    auth: IPCAuth | None = None
    try:
        req = json.loads(input_data)

        # Verify — in the container env, CORPCLAW_IPC_SECRET is injected
        auth = IPCAuth()
        payload = auth.verify(req)

        if payload.get("type") != "tool_call":
            raise ValueError("Unknown payload type")

        tool_name = payload.get("tool")
        args = payload.get("args", {})

        if not isinstance(tool_name, str):
            raise ValueError("Missing or invalid 'tool' field")
        if not isinstance(args, dict):
            raise ValueError("'args' must be a dict")

        registry = get_registry()
        tool = registry.get(tool_name)
        if tool is None:
            available = list(registry.items().keys())
            raise ValueError(f"Unknown tool: {tool_name}. Available: {available}")

        result = asyncio.run(tool.execute(**args))
        response_payload = {"status": "success", "result": result}

    except Exception as e:
        response_payload = {"status": "error", "error": str(e)}

    finally:
        # Sign and respond
        try:
            if auth is None:
                auth = IPCAuth()
            signed_resp = auth.sign(response_payload)
            print(json.dumps(signed_resp))
        except Exception:
            print(
                json.dumps(
                    {
                        "signature": "",
                        "nonce": "",
                        "timestamp": "0",
                        "payload": {
                            "status": "error",
                            "error": "Fatal IPC auth error in container",
                        },
                    }
                )
            )


if __name__ == "__main__":
    process_request()
