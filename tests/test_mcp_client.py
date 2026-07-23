"""Tests for MCP Client."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.extensions.mcp.client import MCPClient, MCPClientError


@pytest.fixture
def mock_process():
    process = AsyncMock()
    process.stdin = MagicMock()
    process.stdin.drain = AsyncMock()
    process.stdout = AsyncMock()
    process.terminate = MagicMock()
    process.wait = AsyncMock()
    return process


@pytest.mark.asyncio
async def test_mcp_client_connect_disconnect(mock_process):
    client = MCPClient(timeout=1.0)

    with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
        # Mock the response for the initialization (read returns the full line)
        mock_process.stdout.read.return_value = (
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            ).encode("utf-8")
            + b"\n"
        )

        await client.connect(["npx", "server"])
        mock_exec.assert_awaited_once()

        # Test disconnect
        await client.disconnect()
        mock_process.terminate.assert_called_once()
        mock_process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_client_list_tools(mock_process):
    client = MCPClient()
    client._process = mock_process

    tools_call_result = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [{"name": "my_tool", "description": "desc", "inputSchema": {"type": "object"}}]
        },
    }
    mock_process.stdout.read.return_value = json.dumps(tools_call_result).encode("utf-8") + b"\n"

    tools = await client.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "my_tool"


@pytest.mark.asyncio
async def test_mcp_client_call_tool(mock_process):
    client = MCPClient()
    client._process = mock_process

    call_result = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "result text!"}]},
    }
    mock_process.stdout.read.return_value = json.dumps(call_result).encode("utf-8") + b"\n"

    res = await client.call_tool("my_tool", {})
    assert res == "result text!"


@pytest.mark.asyncio
async def test_mcp_client_timeout():
    client = MCPClient(timeout=0.1, total_timeout=0.2)
    process = AsyncMock()
    process.stdin = MagicMock()
    process.stdin.drain = AsyncMock()
    process.stdout = AsyncMock()

    # Simulate a timeout on read (the bounded-line reader)
    async def slow_read(n):  # noqa: ARG001
        await asyncio.sleep(0.5)
        return b""

    process.stdout.read.side_effect = slow_read
    client._process = process

    with pytest.raises(MCPClientError, match="did not respond within"):
        await client.call_tool("tool", {})


@pytest.mark.asyncio
async def test_mcp_client_oversize_response_rejected(mock_process):
    """S3-04: a response line exceeding the byte cap is rejected, not buffered unbounded."""
    client = MCPClient(timeout=1.0, max_response_bytes=64)
    client._process = mock_process

    # read() keeps returning non-newline data past the cap.
    async def feed_no_newline(n):  # noqa: ARG001
        return b"x" * 4096

    mock_process.stdout.read.side_effect = feed_no_newline

    with pytest.raises(MCPClientError, match="exceeded"):
        await client.call_tool("tool", {})
