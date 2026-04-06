from __future__ import annotations

from typing import TYPE_CHECKING, Any

from corpclaw_lite.agent.vision import VisionProcessor
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.builtin.files import (
    IMAGE_EXTENSIONS,
    resolve_and_validate_path,
)

__all__ = [
    "ReadImageTool",
]

if TYPE_CHECKING:
    from corpclaw_lite.users.models import User


class ReadImageTool(Tool):
    """Tool to read the contents of an image file and get a description from the vision model."""

    name = "read_image"
    description = "Read an image file (.png, .jpg, etc.) and return a textual description."
    params = [
        ToolParam(
            name="path",
            type="string",
            description="Relative or absolute path to the image file",
        ),
        ToolParam(
            name="prompt",
            type="string",
            description="What to ask the vision model about this image",
        ),
    ]
    risk_level = RiskLevel.LOW
    terminal = True  # Vision response goes directly to user, no LLM re-paraphrase

    def __init__(self, processor: VisionProcessor, workspace_base: Any | None = None) -> None:
        self._processor = processor
        # workspace_base is the base directory for per-user workspaces
        # (e.g. /project/workspaces). When set, relative paths are resolved
        # against the uploading user's workspace folder so that an agent
        # receiving "image_20260406_181307.jpg" correctly finds the file at
        # workspaces/user_<telegram_id>/image_20260406_181307.jpg.
        self._workspace_base = workspace_base

    async def execute(self, user: User | None = None, **kwargs: Any) -> str:
        path = kwargs.get("path")
        prompt = kwargs.get("prompt", "Describe this image in detail.")

        if not isinstance(path, str) or not isinstance(prompt, str):
            return "Error: missing required parameter 'path' or 'prompt'"

        from pathlib import Path

        try:
            resolved: Path

            # Docker container mounts the user workspace as /workspace.
            # File tools (list_files, read_file) run inside the container and
            # return /workspace/... paths. ReadImageTool is a HOST tool, so we
            # must translate container paths to the equivalent host path.
            _CONTAINER_WS = "/workspace"
            path_str = path.strip()
            if (
                self._workspace_base is not None
                and user is not None
                and user.telegram_id is not None
                and (
                    path_str == _CONTAINER_WS
                    or path_str.startswith(_CONTAINER_WS + "/")
                    or path_str.startswith(_CONTAINER_WS + "\\")
                )
            ):
                # Strip /workspace prefix and resolve against host workspace
                relative = path_str[len(_CONTAINER_WS):].lstrip("/\\")
                user_workspace = Path(self._workspace_base) / f"user_{user.telegram_id}"
                resolved = (user_workspace / relative).resolve()

            elif (
                not Path(path_str).is_absolute()
                and self._workspace_base is not None
                and user is not None
                and user.telegram_id is not None
            ):
                # Relative path → resolve against user workspace
                user_workspace = Path(self._workspace_base) / f"user_{user.telegram_id}"
                resolved = (user_workspace / path_str).resolve()

            else:
                # Absolute non-container path or CLI mode
                resolved = resolve_and_validate_path(path_str)

            if not resolved.exists() or not resolved.is_file():
                return f"Error: Image File '{resolved}' does not exist or is not a file."

            if resolved.suffix.lower() not in IMAGE_EXTENSIONS:
                return f"Error: read_image only accepts image files: {IMAGE_EXTENSIONS}"

            # Delegate to the VisionProcessor (which makes a separate LLM call)
            return await self._processor.describe(resolved, prompt, user)
        except Exception as e:
            return f"Error reading image '{path}': {e}"
