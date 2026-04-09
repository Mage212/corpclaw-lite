"""exec_script — shell command execution tool with timeout support.

Security relies on ToolGuard YAML rules and container isolation.
No hardcoded blocklist — it was removed because blocklists are bypassable
(e.g. ``python3 -c "import shutil; shutil.rmtree('/')"``) and create
false confidence. ToolGuard + containers are the only effective controls.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

__all__ = [
    "DEFAULT_TIMEOUT",
    "ExecScriptTool",
    "MAX_OUTPUT_BYTES",
    "MAX_TIMEOUT",
]

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_OUTPUT_BYTES = 50_000


class ExecScriptTool(Tool):
    """Execute a shell command in the workspace directory."""

    name = "exec_script"
    description = "Execute a shell command in the workspace directory."
    params = [
        ToolParam(name="script", type="string", description="Shell command to execute"),
        ToolParam(
            name="timeout",
            type="integer",
            description="Timeout in seconds (default 30, max 120)",
            required=False,
        ),
    ]
    risk_level = RiskLevel.HIGH

    async def execute(self, **kwargs: Any) -> str:
        script = kwargs.get("script")
        if not script or not isinstance(script, str):
            return "Error: 'script' parameter is required."

        timeout_val = kwargs.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout_val, int):
            timeout_val = DEFAULT_TIMEOUT
        timeout_val = max(1, min(timeout_val, MAX_TIMEOUT))

        workspace = Path.cwd().resolve()

        try:
            proc_kwargs: dict[str, Any] = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": str(workspace),
            }
            if sys.platform != "win32":
                # Unix: new session allows killing the entire process group
                proc_kwargs["start_new_session"] = True

            proc = await asyncio.create_subprocess_shell(script, **proc_kwargs)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_val)
            except TimeoutError:
                # Kill the process (and its group on Unix) on timeout
                try:
                    if sys.platform != "win32":
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # type: ignore[attr-defined]
                    else:
                        proc.kill()
                except (ProcessLookupError, OSError):
                    proc.kill()
                await proc.wait()
                return f"Error: Command timed out after {timeout_val}s"

            output_parts: list[str] = []
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                output_parts.append(stderr.decode("utf-8", errors="replace"))

            output = "\n".join(output_parts)
            if len(output) > MAX_OUTPUT_BYTES:
                output = output[:MAX_OUTPUT_BYTES] + "\n... (truncated)"

            code = proc.returncode if proc.returncode is not None else -1
            return f"Exit code: {code}\n\n{output}"

        except Exception as e:
            return f"Error: {e}"
