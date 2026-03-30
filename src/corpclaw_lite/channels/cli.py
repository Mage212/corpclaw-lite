from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from corpclaw_lite.channels.base import Channel
from corpclaw_lite.users.models import User

__all__ = [
    "CLIChannel",
]


class CLIChannel(Channel):
    """Command-line interface channel."""

    name = "cli"

    def __init__(self) -> None:
        self.console = Console()

    async def start(self) -> None:
        """Start the CLI channel."""
        self.console.print("[bold green]CLI Channel started.[/bold green]")

    async def stop(self) -> None:
        """Stop the CLI channel."""
        self.console.print("[bold yellow]CLI Channel stopped.[/bold yellow]")

    async def send_message(self, user: User, text: str, **opts: Any) -> None:
        """Print a message to the console using rich markdown rendering."""
        # Simple separation
        self.console.print(f"\n[bold blue]Agent (for {user.name}):[/bold blue]")
        self.console.print(Markdown(text))
        self.console.print()

    async def send_file(self, user: User, path: Path, caption: str = "") -> None:
        """Simulate sending a file."""
        self.console.print(f"\n[bold magenta]Sending file to {user.name}:[/bold magenta] {path}")
        if caption:
            self.console.print(f"[dim]{caption}[/dim]")
        self.console.print()

    async def request_approval(self, user: User, action: str, details: str) -> bool:
        """Prompt user for approval synchronously in the terminal."""
        self.console.print(f"\n[bold red]Action requires approval:[/bold red] {action}")
        self.console.print(f"[dim]{details}[/dim]")

        # In a real async CLI, you'd use loop.run_in_executor for input(),
        # but for phase 1 synchronous block is fine for this minimal channel.
        loop = asyncio.get_running_loop()

        def _get_input() -> str:
            return input("Approve? [y/N]: ").strip().lower()

        result = await loop.run_in_executor(None, _get_input)
        return result in ("y", "yes")
