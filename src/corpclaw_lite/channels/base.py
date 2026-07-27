from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from corpclaw_lite.users.models import User

__all__ = [
    "Channel",
]


class Channel(Protocol):
    """Protocol for communication channels (CLI, Telegram, etc)."""

    name: str

    async def start(self) -> None:
        """Initialize the channel connection or interface."""
        ...

    async def stop(self) -> None:
        """Tear down the channel connection."""
        ...

    async def send_message(self, user: User, text: str, **opts: Any) -> Any:
        """Send a plain text message to the channel.

        Return type is ``Any`` rather than ``None`` so concrete channels may
        optionally return the sent message object (e.g. Telegram returns the
        last ``Message`` so callers can attach inline-button callbacks keyed on
        its ``message_id``). Callers that ignore the return value are unaffected.
        """
        ...

    async def send_file(self, user: User, path: Path, caption: str = "") -> None:
        """Send a file attachment to the channel."""
        ...

    async def request_approval(self, user: User, action: str, details: str) -> bool:
        """Request user approval for an action (e.g. dangerous tool call)."""
        ...
