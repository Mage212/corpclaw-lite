from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.builtin.files import resolve_and_validate_path

if TYPE_CHECKING:
    from corpclaw_lite.users.models import User

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


class SendFileTool(Tool):
    """Send a file from the workspace to the user via the communication channel."""

    name = "send_file"
    description = "Send a file from the workspace to the user."
    params = [
        ToolParam(
            name="path",
            type="string",
            description="Path to the file to send (relative to workspace)",
        ),
        ToolParam(
            name="caption",
            type="string",
            description="Optional caption for the file",
            required=False,
        ),
    ]
    risk_level = RiskLevel.MEDIUM

    def __init__(
        self,
        send_callback: Callable[[Path, User, str], Awaitable[str]],
    ) -> None:
        self._send = send_callback

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        path = kwargs.get("path")
        caption = kwargs.get("caption", "")

        if not isinstance(path, str):
            return "Error: 'path' is a required string parameter."
        if not isinstance(caption, str):
            caption = ""

        if user is None:
            return "Error: User context is required for send_file."

        try:
            resolved = resolve_and_validate_path(path)
        except PermissionError as e:
            return f"Error: {e}"

        if not resolved.exists() or not resolved.is_file():
            return f"Error: File '{path}' does not exist."

        size = resolved.stat().st_size
        if size > MAX_FILE_SIZE:
            return f"Error: File too large ({size} bytes, max {MAX_FILE_SIZE})."

        try:
            return await self._send(resolved, user, caption)
        except Exception as e:
            return f"Error sending file '{path}': {e}"
