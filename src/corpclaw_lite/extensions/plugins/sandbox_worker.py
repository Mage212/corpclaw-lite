"""Subprocess worker for loading and executing plugin tool.py in isolation.

Usage::

    # Introspect mode — print tool schema as JSON and exit
    python sandbox_worker.py <tool_path> --introspect

    # Interactive mode — newline-delimited JSON-RPC over stdin/stdout
    python sandbox_worker.py <tool_path>

JSON-RPC commands (interactive mode):
    introspect  — return tool schema
    execute     — call tool.execute(**kwargs), return result string
    shutdown    — terminate worker
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from corpclaw_lite.extensions.tools.base import Tool


def _load_tool(tool_path: str) -> Tool:
    path = Path(tool_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Tool file not found: {path}")

    spec = importlib.util.spec_from_file_location("_plugin_tool", path)
    if not spec or not spec.loader:
        raise ImportError(f"Cannot create module spec for {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if isinstance(attr, type) and issubclass(attr, Tool) and attr is not Tool:
            return attr()

    raise TypeError(f"No Tool subclass found in {path}")


def _introspect(tool: Tool) -> dict[str, Any]:
    params_data: list[dict[str, Any]] = []
    for p in tool.params:
        pd: dict[str, Any] = {
            "name": p.name,
            "type": p.type,
            "description": p.description,
            "required": p.required,
        }
        if p.enum is not None:
            pd["enum"] = p.enum
        params_data.append(pd)

    return {
        "name": tool.name,
        "description": tool.description,
        "params": params_data,
        "risk_level": tool.risk_level.value,
        "parallel_safe": tool.parallel_safe,
        "terminal": tool.terminal,
    }


def _run_introspect(tool_path: str) -> None:
    tool = _load_tool(tool_path)
    print(json.dumps(_introspect(tool)), flush=True)


def _run_interactive(tool_path: str) -> None:
    tool = _load_tool(tool_path)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
            print(json.dumps(resp), flush=True)
            continue

        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method == "introspect":
            result = _introspect(tool)
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}), flush=True)
        elif method == "execute":
            kwargs = params.get("kwargs", {})
            try:
                result = asyncio.run(tool.execute(**kwargs))
                resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -1, "message": str(e)}}
            print(json.dumps(resp), flush=True)
        elif method == "shutdown":
            break
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
            print(json.dumps(resp), flush=True)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(
            json.dumps({"error": "Usage: sandbox_worker.py <tool_path> [--introspect]"}), flush=True
        )
        sys.exit(1)

    tool_path = args[0]
    introspect_only = "--introspect" in args

    try:
        if introspect_only:
            _run_introspect(tool_path)
        else:
            _run_interactive(tool_path)
    except Exception as e:
        print(json.dumps({"error": str(e)}), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
