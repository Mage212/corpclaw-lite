from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.channels.cli import CLIChannel
from corpclaw_lite.config.loader import load_settings
from corpclaw_lite.extensions.tools.builtin.files import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.openai import OpenAIProvider
from corpclaw_lite.llm.anthropic import AnthropicProvider
from corpclaw_lite.llm.routing import ProviderRouter
from corpclaw_lite.users.models import User

app = typer.Typer(help="CorpClaw Lite CLI")
console = Console()


def _get_registry() -> ToolRegistry:
    """Setup a registry with default Phase 1 tools."""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(ListFilesTool())
    registry.register(SearchFilesTool())
    return registry


@app.command()
def chat(
    config_path: str = typer.Option("config/settings.yaml", "--config", "-c", help="Path to YAML settings file"),
    provider_name: Optional[str] = typer.Option(None, "--provider", "-p", help="Provider name from settings (e.g. local or cloud)"),
) -> None:
    """Start an interactive chat session with the agent."""
    
    settings = load_settings(config_path)
    router = ProviderRouter(settings.llm)
    
    # Simple direct provider instantiation based on type
    p_name = provider_name or settings.llm.default
    provider_settings = router.get_provider_settings(task_kind=p_name)  # Here we just abuse it to get by name if needed... wait, router._get_named(p_name) is private.
    
    # Clean way to get by name: just read from named dict directly since we're in Phase 1
    if p_name not in settings.llm.named:
        console.print(f"[bold red]Error:[/bold red] Provider '{p_name}' not found in settings.")
        raise typer.Exit(1)
        
    p_settings = settings.llm.named[p_name]
    
    if p_settings.type == "openai":
        provider = OpenAIProvider(p_settings)
    elif p_settings.type == "anthropic":
        provider = AnthropicProvider(p_settings)
    else:
        console.print(f"[bold red]Error:[/bold red] Unsupported provider type '{p_settings.type}'.")
        raise typer.Exit(1)

    registry = _get_registry()
    loop = AgentLoop(provider, registry, settings.agent)
    channel = CLIChannel()
    
    # Phase 1: simple dummy user
    user = User(id=0, name="CLI User", department="admin")

    async def run_chat() -> None:
        await channel.start()
        
        while True:
            try:
                user_input = input("\nYou: ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ("exit", "quit"):
                    break
                    
                result = await loop.run(user, user_input)
                await channel.send_message("cli", result)
            except KeyboardInterrupt:
                break
            except Exception as e:
                console.print(f"[bold red]Runtime Error:[/bold red] {e}")

        await channel.stop()

    try:
        asyncio.run(run_chat())
    except KeyboardInterrupt:
        console.print("\n[dim]Session terminated by user.[/dim]")


if __name__ == "__main__":
    app()
