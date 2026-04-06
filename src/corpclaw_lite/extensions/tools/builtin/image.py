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
            path_obj = Path(path)

            # If path is relative AND we have a workspace_base AND a user with
            # telegram_id, resolve against the user's workspace directory.
            if (
                not path_obj.is_absolute()
                and self._workspace_base is not None
                and user is not None
                and user.telegram_id is not None
            ):
                user_workspace = Path(self._workspace_base) / f"user_{user.telegram_id}"
                resolved = (user_workspace / path_obj).resolve()
            else:
                # Fallback: CWD-relative (CLI mode or absolute paths)
                resolved = resolve_and_validate_path(path)

            if not resolved.exists() or not resolved.is_file():
                return f"Error: Image File '{resolved}' does not exist or is not a file."

            if resolved.suffix.lower() not in IMAGE_EXTENSIONS:
                return f"Error: read_image only accepts image files: {IMAGE_EXTENSIONS}"

            # Delegate to the VisionProcessor (which makes a separate LLM call)
            return await self._processor.describe(resolved, prompt, user)
        except Exception as e:
            return f"Error reading image '{path}': {e}"
