"""Tests for MCP client, adapter, and manager."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.extensions.mcp.adapter import MCPToolAdapter
from corpclaw_lite.extensions.mcp.client import MCPClient, MCPToolDef
from corpclaw_lite.extensions.mcp.manager import MCPManager
from corpclaw_lite.extensions.tools.registry import ToolRegistry


def _make_tool_def(name: str = "test_tool", description: str = "A test tool") -> MCPToolDef:
    return MCPToolDef(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
            },
            "required": ["path"],
        },
    )


# ── Adapter tests ──────────────────────────────────────────────────────────────


def test_mcp_adapter_name_and_description() -> None:
    tool_def = _make_tool_def("read_file", "Reads a file")
    client = MagicMock(spec=MCPClient)
    adapter = MCPToolAdapter(tool_def=tool_def, client=client)

    assert adapter.name == "read_file"
    assert adapter.description == "Reads a file"


def test_mcp_adapter_params_from_schema() -> None:
    tool_def = _make_tool_def()
    client = MagicMock(spec=MCPClient)
    adapter = MCPToolAdapter(tool_def=tool_def, client=client)

    params = adapter.params
    assert len(params) == 1
    assert params[0].name == "path"
    assert params[0].type == "string"
    assert params[0].required is True


@pytest.mark.asyncio
async def test_mcp_adapter_execute_calls_client() -> None:
    tool_def = _make_tool_def("echo_tool", "Echoes input")
    client = MagicMock(spec=MCPClient)
    client.call_tool = AsyncMock(return_value="echo result")
    adapter = MCPToolAdapter(tool_def=tool_def, client=client)

    result = await adapter.execute(path="/tmp/test.txt")

    client.call_tool.assert_called_once_with("echo_tool", {"path": "/tmp/test.txt"})
    assert result == "echo result"


@pytest.mark.asyncio
async def test_mcp_adapter_execute_handles_error() -> None:
    tool_def = _make_tool_def("broken_tool")
    client = MagicMock(spec=MCPClient)
    client.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))
    adapter = MCPToolAdapter(tool_def=tool_def, client=client)

    result = await adapter.execute()

    assert "connection lost" in result
    assert "broken_tool" in result


# ── Manager tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_manager_no_config(tmp_path: Any) -> None:
    """Manager silently skips when config file does not exist."""
    manager = MCPManager(config_path=tmp_path / "nonexistent.yaml")
    registry = ToolRegistry()
    count = await manager.connect_all(registry)
    assert count == 0
    assert registry.list_all() == []


@pytest.mark.asyncio
async def test_mcp_manager_registers_tools(tmp_path: Any) -> None:
    """Manager reads config, connects to server, and registers tools in registry."""
    config = tmp_path / "mcp_servers.yaml"
    config.write_text(
        "servers:\n  - name: test-server\n    command: ['echo', 'mcp']\n", encoding="utf-8"
    )

    mock_client = MagicMock(spec=MCPClient)
    mock_client.connect = AsyncMock()
    mock_client.list_tools = AsyncMock(return_value=[_make_tool_def("mcp_read", "Read via MCP")])
    mock_client.disconnect = AsyncMock()

    registry = ToolRegistry()

    with patch("corpclaw_lite.extensions.mcp.manager.MCPClient", return_value=mock_client):
        manager = MCPManager(config_path=config)
        count = await manager.connect_all(registry)

    assert count == 1
    assert registry.get("mcp_read") is not None

    await manager.disconnect_all()
    mock_client.disconnect.assert_called_once()
