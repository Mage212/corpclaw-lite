"""Tests for MCPHotReloader — config file change detection and diff-based reconnect."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.extensions.mcp.manager import MCPManager
from corpclaw_lite.extensions.mcp.watcher import MCPHotReloader
from corpclaw_lite.extensions.tools.registry import ToolRegistry


def _write_config(path: Path, servers: list[dict[str, Any]]) -> None:
    import yaml

    path.write_text(yaml.dump({"servers": servers}), encoding="utf-8")


@pytest.mark.asyncio
async def test_hotreloader_no_reload_on_same_mtime(tmp_path: Path) -> None:
    """If mtime hasn't changed, no reload should happen."""
    cfg = tmp_path / "mcp_servers.yaml"
    _write_config(cfg, [])

    manager = MagicMock(spec=MCPManager)
    registry = ToolRegistry()
    reloader = MCPHotReloader(cfg, manager, registry, poll_interval=1.0)

    # First check records mtime but does not reload
    await reloader._check()
    # Second check — same mtime — no reload
    await reloader._check()

    manager.load_config_raw.assert_not_called()


@pytest.mark.asyncio
async def test_hotreloader_detects_mtime_change(tmp_path: Path) -> None:
    """When mtime increases, _reload() should be called."""
    cfg = tmp_path / "mcp_servers.yaml"
    _write_config(cfg, [])

    manager = MagicMock(spec=MCPManager)
    manager.get_server_names.return_value = []
    manager.load_config_raw.return_value = []
    registry = ToolRegistry()
    reloader = MCPHotReloader(cfg, manager, registry, poll_interval=1.0)

    # First check captures mtime
    await reloader._check()

    # Simulate file change by modifying mtime

    reloader._last_mtime = reloader._last_mtime - 1  # type: ignore[operator]

    await reloader._check()
    manager.load_config_raw.assert_called_once()


@pytest.mark.asyncio
async def test_hotreloader_adds_new_server(tmp_path: Path) -> None:
    """New server in config → reconnect_server called."""
    cfg = tmp_path / "mcp_servers.yaml"
    _write_config(cfg, [{"name": "new-srv", "command": "echo", "args": []}])

    manager = MagicMock(spec=MCPManager)
    manager.get_server_names.return_value = []  # no servers connected yet
    manager.load_config_raw.return_value = [{"name": "new-srv", "command": "echo", "args": []}]
    manager.reconnect_server = AsyncMock(return_value=2)
    registry = ToolRegistry()

    reloader = MCPHotReloader(cfg, manager, registry)
    # Force a reload
    await reloader._reload()

    manager.reconnect_server.assert_awaited_once()
    call_args = manager.reconnect_server.call_args[0]
    assert call_args[0]["name"] == "new-srv"


@pytest.mark.asyncio
async def test_hotreloader_removes_deleted_server(tmp_path: Path) -> None:
    """Server removed from config → disconnect_server called."""
    cfg = tmp_path / "mcp_servers.yaml"
    _write_config(cfg, [])  # empty — no servers

    manager = MagicMock(spec=MCPManager)
    manager.get_server_names.return_value = ["old-srv"]  # was connected
    manager.load_config_raw.return_value = []
    manager.disconnect_server = AsyncMock()
    registry = ToolRegistry()

    reloader = MCPHotReloader(cfg, manager, registry)
    await reloader._reload()

    manager.disconnect_server.assert_awaited_once_with("old-srv", registry)


@pytest.mark.asyncio
async def test_hotreloader_reconnects_existing_server(tmp_path: Path) -> None:
    """Server present in both old and new config → reconnect_server called."""
    cfg = tmp_path / "mcp_servers.yaml"
    _write_config(cfg, [{"name": "srv", "command": "npx", "args": ["-y", "pkg"]}])

    manager = MagicMock(spec=MCPManager)
    manager.get_server_names.return_value = ["srv"]
    manager.load_config_raw.return_value = [
        {"name": "srv", "command": "npx", "args": ["-y", "pkg"]}
    ]
    manager.reconnect_server = AsyncMock(return_value=1)
    registry = ToolRegistry()

    reloader = MCPHotReloader(cfg, manager, registry)
    await reloader._reload()

    manager.reconnect_server.assert_awaited_once()
