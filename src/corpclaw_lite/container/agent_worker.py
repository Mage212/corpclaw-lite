"""Container-side agent worker — executes tools inside Docker containers.

Reads a signed IPC request from stdin, executes the requested tool,
signs the response, and writes it to stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from corpclaw_lite.security.ipc_auth import IPCAuth

if TYPE_CHECKING:
    from corpclaw_lite.extensions.tools.registry import ToolRegistry

__all__ = ["process_request"]

# Fallback when the IPC payload does not include tool_timeout.
# Should not happen in normal operation (ContainerIPC always sends it),
# but acts as a safety net for ad-hoc calls or older host versions.
_DEFAULT_TOOL_TIMEOUT = 25.0

# Where the persistent nonce store lives inside the container. Set into the
# container env by container/policies.py; lives on the writable /tmp tmpfs so
# it survives across the short-lived docker-exec processes sharing one
# container (H-1, code review). Not a secret — safe in the long-lived env.
_DEFAULT_NONCE_STORE_PATH = "/tmp/corpclaw_nonces.db"


def _resolve_nonce_store_path() -> Path | None:
    """Resolve the persistent nonce-store path, or None to use in-memory.

    Reads ``CORPCLAW_IPC_NONCE_STORE`` (set by container/policies.py). Returns
    None when explicitly empty so host-side callers without the env var keep
    the legacy in-memory behaviour.
    """
    raw = os.environ.get("CORPCLAW_IPC_NONCE_STORE")
    if raw is None:
        return Path(_DEFAULT_NONCE_STORE_PATH)
    if raw == "":
        return None
    return Path(raw)


def _init_logging() -> None:
    logging.basicConfig(level=logging.ERROR)


def _build_container_registry() -> ToolRegistry:
    """Build a ToolRegistry with tools available inside containers.

    Lazy-loading tools here to speed up python startup in the container.
    """
    try:
        from corpclaw_lite.extensions.tools.builtin.apply_fill_plan import ApplyFillPlanTool
        from corpclaw_lite.extensions.tools.builtin.chart_generate import ChartGenerateTool
        from corpclaw_lite.extensions.tools.builtin.convert_format import ConvertFormatTool
        from corpclaw_lite.extensions.tools.builtin.diff_text import DiffTextTool
        from corpclaw_lite.extensions.tools.builtin.excel import NormalizeExcelTool
        from corpclaw_lite.extensions.tools.builtin.excel_inspect import ExcelInspectTool
        from corpclaw_lite.extensions.tools.builtin.excel_workbook import ExcelWorkbookTool
        from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool
        from corpclaw_lite.extensions.tools.builtin.files import (
            EditFileTool,
            ListFilesTool,
            ReadFileTool,
            SearchFilesTool,
            WriteFileTool,
        )
        from corpclaw_lite.extensions.tools.builtin.pdf_reader import PdfReaderTool
        from corpclaw_lite.extensions.tools.builtin.table_query import TableQueryTool
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
        DiffTextTool(),
        ConvertFormatTool(),
        TableQueryTool(),
        ChartGenerateTool(),
        PdfReaderTool(),
        ExcelInspectTool(),
        ExcelWorkbookTool(),
        # Brief→FillPlan production path.
        # Must be kept in sync with factory._all_tool_classes(); otherwise
        # container.enabled=true rejects the tool call the LLM emits after
        # reading FILES_BRIEF.
        ApplyFillPlanTool(),
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
    # Two-line stdin protocol (security-hardening sprint 3): line 1 is the IPC
    # secret, line 2 is the signed JSON payload. Keeping the secret on stdin
    # keeps it out of the docker exec argv (visible via ps/proc). We fall back to
    # CORPCLAW_IPC_SECRET env only if the first line looks like JSON (legacy host
    # callers that still send a single payload line and rely on env).
    first_line = sys.stdin.readline()
    second_line = sys.stdin.readline()
    secret_from_stdin = first_line.strip()
    input_data = second_line.strip()
    if not input_data:
        # Legacy single-line caller: the first line was actually the payload.
        input_data = secret_from_stdin
        secret_from_stdin = ""

    if not input_data:
        return

    response_payload: dict[str, str] = {"status": "error", "error": "Unknown error"}
    auth: IPCAuth | None = None
    try:
        req = json.loads(input_data)

        # Verify — secret is provided on stdin for this docker-exec process only,
        # not in the long-lived container create environment.
        #
        # H-1 (code review): use a persistent nonce store so replay protection
        # works across the short-lived docker-exec processes that share one
        # running container. An in-memory store would start empty on every call
        # and never detect a replayed request within the 300s TTL. The path is
        # set into the container env by container/policies.py and lives on the
        # writable /tmp tmpfs. If the file cannot be opened, IPCAuth degrades to
        # an in-memory store (verification still works, just without cross-
        # process replay protection).
        nonce_store_path = _resolve_nonce_store_path()
        auth = IPCAuth(
            secret=secret_from_stdin or None,
            nonce_store_path=nonce_store_path,
        )
        payload = auth.verify(req)

        if payload.get("type") != "tool_call":
            raise ValueError("Unknown payload type")

        tool_name = payload.get("tool")
        args = payload.get("args", {})
        tool_timeout = float(payload.get("tool_timeout", _DEFAULT_TOOL_TIMEOUT))

        if not isinstance(tool_name, str):
            raise ValueError("Missing or invalid 'tool' field")
        if not isinstance(args, dict):
            raise ValueError("'args' must be a dict")

        registry = get_registry()
        tool = registry.get(tool_name)
        if tool is None:
            available = list(registry.items().keys())
            raise ValueError(f"Unknown tool: {tool_name}. Available: {available}")

        async def _run_with_timeout() -> str:
            return await asyncio.wait_for(tool.execute(**args), timeout=tool_timeout)

        result = asyncio.run(_run_with_timeout())
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
