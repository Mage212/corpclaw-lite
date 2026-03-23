"""Container-side agent worker — executes tools inside Docker containers.

Reads a signed IPC request from stdin, executes the requested tool,
signs the response, and writes it to stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool
from corpclaw_lite.extensions.tools.builtin.files import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.security.ipc_auth import IPCAuth

logging.basicConfig(level=logging.ERROR)


def _build_container_registry() -> ToolRegistry:
    """Build a ToolRegistry with tools available inside containers.

    This is a subset of the full registry — only tools that make sense
    in a sandboxed container environment (file ops + shell execution).
    Host-side tools (send_file, memory_*, web_fetch, read_image, dispatch_subagent)
    are excluded.
    """
    registry = ToolRegistry()
    for tool in [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
        ExecScriptTool(),
    ]:
        registry.register(tool)
    return registry


registry = _build_container_registry()


def process_request() -> None:
    """Read from stdin, verify, execute tool, sign response, print to stdout."""
    input_data = sys.stdin.read()
    if not input_data:
        return

    response_payload: dict[str, str] = {"status": "error", "error": "Unknown error"}
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

        # Look up and execute the tool
        tool = registry.get(tool_name)
        if tool is None:
            raise ValueError(f"Unknown tool: {tool_name}")

        result = asyncio.run(tool.execute(**args))
        response_payload = {"status": "success", "result": result}

    except Exception as e:
        response_payload = {"status": "error", "error": str(e)}

    finally:
        # Sign and respond
        try:
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
