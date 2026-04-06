from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.builtin.files import resolve_and_validate_path

__all__ = [
    "MAX_FILE_SIZE",
    "SendFileTool",
]

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
        workspace_base: Any | None = None,
    ) -> None:
        self._send = send_callback
        self._workspace_base = workspace_base

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
            resolved: Path
            path_str = path.strip()

            # Docker container mounts user workspace as /workspace.
            # Agent tools (list_files, etc.) return /workspace/... paths.
            # SendFileTool is a HOST tool — translate container paths.
            _CONTAINER_WS = "/workspace"
            if (
                self._workspace_base is not None
                and user.telegram_id is not None
                and (
                    path_str == _CONTAINER_WS
                    or path_str.startswith(_CONTAINER_WS + "/")
                    or path_str.startswith(_CONTAINER_WS + "\\")
                )
            ):
                relative = path_str[len(_CONTAINER_WS) :].lstrip("/\\")
                user_workspace = Path(self._workspace_base) / f"user_{user.telegram_id}"
                resolved = (user_workspace / relative).resolve()

            elif (
                not Path(path_str).is_absolute()
                and self._workspace_base is not None
                and user.telegram_id is not None
            ):
                # Relative path → resolve against user workspace
                user_workspace = Path(self._workspace_base) / f"user_{user.telegram_id}"
                resolved = (user_workspace / path_str).resolve()

            else:
                resolved = resolve_and_validate_path(path_str)

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
