"""ContainerIPC — per-call docker exec IPC for container tool dispatch.

Design:
    - Each tool call is a stateless `docker exec` into the user's running container
    - The request JSON is HMAC-signed before being piped to agent_worker.py's stdin
    - The response is verified before returning to the caller

This stateless pattern is more resilient than a persistent stdio connection:
    - Container crash → next call gets ContainerManagerError → user sees clean error
    - No "zombie" processes or broken pipe races
    - Each call has its own timeout — no head-of-line blocking

Sequence:
    IPCToolProxy.execute()
        → ContainerIPC.send_tool_call(user_id, tool, args)
            → docker exec -i corpclaw_agent_{user_id} python -m ...agent_worker
                → agent_worker reads signed stdin, executes tool, prints signed JSON
            → verify response signature
        → return result string
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from corpclaw_lite.security.ipc_auth import IPCAuth

__all__ = [
    "ContainerIPC",
    "ContainerIPCError",
]

logger = logging.getLogger(__name__)


class ContainerIPCError(Exception):
    """Raised for errors in container IPC communication."""


class ContainerIPC:
    """Manages stateless docker-exec-based IPC with running Docker containers.

    One instance is shared across all users. The user_id is passed per-call
    to resolve the container name (corpclaw_agent_{user_id}).
    """

    def __init__(self, auth: IPCAuth, timeout_seconds: float = 30.0) -> None:
        self.auth = auth
        self.timeout = timeout_seconds
        self._last_used: dict[int, float] = {}

    @classmethod
    def from_env(cls, timeout_seconds: float = 30.0) -> ContainerIPC:
        """Convenience constructor — IPCAuth reads CORPCLAW_IPC_SECRET from env."""
        return cls(auth=IPCAuth(), timeout_seconds=timeout_seconds)

    @staticmethod
    def container_name(user_id: int) -> str:
        """Return the canonical container name for a user."""
        return f"corpclaw_agent_{user_id}"

    async def send_tool_call(
        self,
        user_id: int,
        tool_name: str,
        args: dict[str, Any],
    ) -> str:
        """Execute a tool inside the user's container and return the result.

        Sends a signed JSON payload to agent_worker.py via docker exec stdin,
        then verifies the signed response from stdout.

        Args:
            user_id: Telegram user ID — resolves to corpclaw_agent_{user_id}.
            tool_name: Name of the tool to execute (must be registered in agent_worker).
            args: Tool arguments dict.

        Returns:
            Tool result string, or an error string if execution failed.
        """
        name = self.container_name(user_id)
        self._last_used[user_id] = time.monotonic()
        payload = {"type": "tool_call", "tool": tool_name, "args": args}
        signed_message = self.auth.sign(payload)
        input_data = (json.dumps(signed_message) + "\n").encode("utf-8")

        cmd = [
            "docker",
            "exec",
            "-i",
            name,
            "python",
            "-m",
            "corpclaw_lite.container.agent_worker",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=input_data), timeout=self.timeout
                )
            except TimeoutError:
                # Kill the docker exec process to prevent orphans
                process.kill()
                await process.wait()
                return f"Error: Container tool '{tool_name}' timed out after {self.timeout}s"

            if process.returncode != 0:
                err_msg = stderr.decode("utf-8").strip()
                logger.error(
                    "ContainerIPC exec error (user=%d, tool=%s): %s",
                    user_id,
                    tool_name,
                    err_msg,
                )
                return f"Container execution error: {err_msg}"

            # Parse and verify signature on response
            try:
                response_str = stdout.decode("utf-8").strip()
                response_msg = json.loads(response_str)
                verified_response = self.auth.verify(response_msg)

                if verified_response.get("status") == "error":
                    return f"Error from container: {verified_response.get('error')}"

                return str(verified_response.get("result", ""))

            except json.JSONDecodeError as e:
                logger.error(
                    "Failed to parse container response for user=%d: '%s'",
                    user_id,
                    stdout.decode("utf-8"),
                )
                return f"Error: Invalid JSON response from container: {e}"
            except Exception as e:
                logger.error("Container signature verification failed (user=%d): %s", user_id, e)
                return "Error: Security verification failed for container response."

        except Exception as e:
            return f"Error in Container IPC: {e}"

    def get_last_used(self, user_id: int) -> float | None:
        """Return the monotonic timestamp of the last IPC call for a user, or None."""
        return self._last_used.get(user_id)
