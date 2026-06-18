"""Tests for MCP client, adapter, and manager."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.departments.manager import DepartmentConfig, DepartmentManager
from corpclaw_lite.departments.permissions import PermissionChecker
from corpclaw_lite.extensions.mcp.adapter import MCPToolAdapter
from corpclaw_lite.extensions.mcp.client import MCPClient, MCPToolDef
from corpclaw_lite.extensions.mcp.manager import MCPManager
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User


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
    assert adapter.source_kind == "mcp"
    assert adapter.source_name == "unknown"


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
    tool = registry.get("mcp_read")
    assert tool is not None
    assert tool.source_kind == "mcp"
    assert tool.source_name == "test-server"

    await manager.disconnect_all()
    mock_client.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_tool_scope_filters_schema_and_execution(tmp_path: Any) -> None:
    config = tmp_path / "mcp_servers.yaml"
    config.write_text(
        "servers:\n  - name: server_a\n    command: ['echo', 'mcp']\n", encoding="utf-8"
    )
    mock_client = MagicMock(spec=MCPClient)
    mock_client.connect = AsyncMock()
    mock_client.list_tools = AsyncMock(return_value=[_make_tool_def("mcp_read", "Read via MCP")])
    mock_client.call_tool = AsyncMock(return_value="mcp result")
    mock_client.disconnect = AsyncMock()
    registry = ToolRegistry()

    with patch("corpclaw_lite.extensions.mcp.manager.MCPClient", return_value=mock_client):
        manager = MCPManager(config_path=config)
        await manager.connect_all(registry)

    mgr = DepartmentManager()
    mgr._departments["engineering"] = DepartmentConfig(
        {
            "description": "Engineering",
            "allowed_tools": ["*"],
            "allowed_mcp": ["server_b"],
        }
    )
    mgr._departments["it"] = DepartmentConfig(
        {
            "description": "IT",
            "allowed_tools": ["*"],
            "allowed_mcp": ["server_a"],
        }
    )
    checker = PermissionChecker(mgr)
    engineering = User(id=1, name="Eng", department="engineering")
    it = User(id=2, name="IT", department="it")

    assert registry.to_schemas_for_user(checker, engineering) == []
    assert len(registry.to_schemas_for_user(checker, it)) == 1

    denied = await registry.execute(
        "mcp_read",
        {"path": "/tmp/a"},
        user=engineering,
        permission_checker=checker,
    )
    assert "Permission denied" in denied
    assert (
        await registry.execute(
            "mcp_read",
            {"path": "/tmp/a"},
            user=it,
            permission_checker=checker,
        )
        == "mcp result"
    )

    await manager.disconnect_all()


def test_mcp_manager_multi_file_merge_by_name(tmp_path: Any) -> None:
    """Multiple config files are merged; a server name in a later file overrides."""
    default_cfg = tmp_path / "mcp_servers.yaml"
    overlay_cfg = tmp_path / "overlay" / "mcp_servers.yaml"
    overlay_cfg.parent.mkdir()

    default_cfg.write_text(
        "servers:\n"
        "  - name: shared\n    command: ['echo', 'default']\n"
        "  - name: only-default\n    command: ['echo', 'd2']\n",
        encoding="utf-8",
    )
    overlay_cfg.write_text(
        "servers:\n"
        "  - name: shared\n    command: ['echo', 'overlay']\n"
        "  - name: only-overlay\n    command: ['echo', 'o2']\n",
        encoding="utf-8",
    )

    manager = MCPManager(config_path=[default_cfg, overlay_cfg])
    merged = manager.load_config_raw()

    by_name = {str(s.get("name")): s for s in merged}
    assert set(by_name) == {"shared", "only-default", "only-overlay"}
    # Overlay wins for "shared".
    assert by_name["shared"]["command"] == ["echo", "overlay"]


def test_mcp_manager_single_path_backward_compat(tmp_path: Any) -> None:
    """Single path (str or Path) still works as before."""
    config = tmp_path / "mcp_servers.yaml"
    config.write_text("servers:\n  - name: s1\n    command: ['echo']\n", encoding="utf-8")

    manager = MCPManager(config_path=config)
    assert manager.config_paths == [config]
    assert len(manager.load_config_raw()) == 1
