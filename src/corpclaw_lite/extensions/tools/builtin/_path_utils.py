"""Shared path resolution utilities for host-side tools.

Docker container mounts the user workspace as /workspace. File tools run
inside the container and return /workspace/... paths. Host-side tools
(ReadImageTool, SendFileTool) must translate container paths to host paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corpclaw_lite.users.models import User

_CONTAINER_WS = "/workspace"


def resolve_container_path(
    path_str: str,
    workspace_base: Path | None,
    user: User | None,
) -> Path:
    """Translate a (possibly container-relative) path to an absolute host path.

    Args:
        path_str: Raw path from tool arguments (may be /workspace/..., relative, or absolute).
        workspace_base: Host-side workspace root (e.g. /path/to/workspaces).
        user: Current user (needed to resolve per-user workspace directory).

    Returns:
        Resolved absolute Path on the host filesystem.
    """
    path_str = path_str.strip()

    if (
        workspace_base is not None
        and user is not None
        and user.telegram_id is not None
        and (
            path_str == _CONTAINER_WS
            or path_str.startswith(_CONTAINER_WS + "/")
            or path_str.startswith(_CONTAINER_WS + "\\")
        )
    ):
        relative = path_str[len(_CONTAINER_WS) :].lstrip("/\\")
        user_workspace = Path(workspace_base) / f"user_{user.telegram_id}"
        return (user_workspace / relative).resolve()

    if (
        not Path(path_str).is_absolute()
        and workspace_base is not None
        and user is not None
        and user.telegram_id is not None
    ):
        user_workspace = Path(workspace_base) / f"user_{user.telegram_id}"
        return (user_workspace / path_str).resolve()

    return Path(path_str).resolve()
