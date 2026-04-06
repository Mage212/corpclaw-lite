"""exec_script — shell command execution tool with timeout support."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

__all__ = [
    "BLOCKED_PATTERNS",
    "DEFAULT_TIMEOUT",
    "ExecScriptTool",
    "MAX_OUTPUT_BYTES",
    "MAX_TIMEOUT",
]

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_OUTPUT_BYTES = 50_000

# Defense-in-depth: block destructive patterns regardless of ToolGuard rules.
# These patterns catch obvious destructive/RCE vectors that must NEVER execute
# in a sandboxed agent context. ToolGuard YAML rules are the primary control --
# these are the last-resort hardcoded backstop.
BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    # Destructive file operations
    re.compile(r"rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)*/\s*$"),  # rm -rf /
    re.compile(r"rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/\b"),  # rm -rf /path
    re.compile(r"find\s+/.*-delete"),  # find / -delete
    re.compile(r"find\s+/.*-exec\s+rm"),  # find / -exec rm
    re.compile(r"mkfs\."),  # filesystem format
    re.compile(r"dd\s+.*of=/dev/"),  # dd to device
    re.compile(r">\s*/dev/sd[a-z]"),  # overwrite disk
    re.compile(r"chmod\s+777\s+/\s*$"),  # chmod 777 /
    # Fork bomb
    re.compile(r":\(\)\{.*\|.*&\s*\};:"),
    # RCE via download-and-execute
    re.compile(r"curl\s+.*\|\s*(ba)?sh", re.IGNORECASE),  # curl | bash
    re.compile(r"wget\s+.*\|\s*(ba)?sh", re.IGNORECASE),  # wget | bash
    re.compile(r"curl\s+.*-o\s*-\s*\|\s*(ba)?sh", re.IGNORECASE),  # curl -o- | bash
    # Obfuscated payload delivery
    re.compile(r"base64\s+(--decode|-d).*\|"),  # base64 decode | pipe
    re.compile(r"\|\s*base64\s+(--decode|-d)"),  # ... | base64 -d | ...
    # Privilege escalation
    re.compile(r"\bsudo\s+"),  # sudo anything
    # Eval with explicit string literal (eval of variable is allowed for scripting)
    re.compile(r"""\beval\s+['"`]"""),  # eval '...' or eval "..."
]


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

        for pat in BLOCKED_PATTERNS:
            if pat.search(script):
                return "Error: command blocked by built-in safety filter."

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
