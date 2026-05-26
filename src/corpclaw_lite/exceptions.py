from __future__ import annotations

__all__ = [
    "CorpClawError",
    "StartupConfigurationError",
    "StorageError",
    "ToolExecutionError",
    "ContainerIPCError",
]


class CorpClawError(Exception):
    """Base for all CorpClaw typed exceptions."""


class StartupConfigurationError(RuntimeError):
    """Raised when the app cannot start with the current local environment."""

    def __init__(self, message: str, *, hint: str | None = None, exit_code: int = 1) -> None:
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        full_message = message if hint is None else f"{message} {hint}"
        super().__init__(full_message)


class StorageError(CorpClawError):
    """Raised when a memory/DB storage operation fails."""


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
