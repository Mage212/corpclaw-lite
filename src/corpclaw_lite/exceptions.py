from __future__ import annotations

__all__ = [
    "CorpClawError",
    "MemoryError",
    "ToolExecutionError",
    "ContainerIPCError",
]


class CorpClawError(Exception):
    """Base for all CorpClaw typed exceptions."""


class MemoryError(CorpClawError):
    """Raised when a memory/DB operation fails."""


class ToolExecutionError(CorpClawError):
    """Raised when a tool execution fails."""

    def __init__(self, tool_name: str, message: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' failed: {message}")


class ContainerIPCError(CorpClawError):
    """Raised when container IPC communication fails."""

    def __init__(self, user_id: int, message: str) -> None:
        self.user_id = user_id
        super().__init__(f"Container IPC error (user={user_id}): {message}")
