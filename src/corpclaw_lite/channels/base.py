from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class Channel(Protocol):
    """Protocol for communication channels (CLI, Telegram, etc)."""

    name: str

    async def start(self) -> None:
        """Initialize the channel connection or interface."""
        ...

    async def stop(self) -> None:
        """Tear down the channel connection."""
        ...

    async def send_message(self, chat_id: str, text: str, **opts: Any) -> None:
        """Send a plain text message to the channel."""
        ...

    async def send_file(self, chat_id: str, path: Path, caption: str = "") -> None:
        """Send a file attachment to the channel."""
        ...

    async def request_approval(self, chat_id: str, action: str, details: str) -> bool:
        """Request user approval for an action (e.g. dangerous tool call)."""
        ...
