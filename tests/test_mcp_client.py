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


# ── H-3 (code review): env filtering for untrusted MCP subprocesses ──────────


def _init_response_bytes() -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}).encode(
            "utf-8"
        )
        + b"\n"
    )


@pytest.mark.asyncio
async def test_mcp_connect_does_not_inherit_secrets(mock_process, monkeypatch):
    """Provider keys and CORPCLAW_IPC_SECRET must never reach an MCP subprocess."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leaked-key-1234567890")
    monkeypatch.setenv("CORPCLAW_IPC_SECRET", "x" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leaked-1234567890")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")

    client = MCPClient(timeout=1.0)
    mock_process.stdout.read.return_value = _init_response_bytes()

    with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
        await client.connect(["npx", "server"])
        passed_env = mock_exec.call_args.kwargs["env"]

    assert "OPENAI_API_KEY" not in passed_env
    assert "CORPCLAW_IPC_SECRET" not in passed_env
    assert "ANTHROPIC_API_KEY" not in passed_env


@pytest.mark.asyncio
async def test_mcp_connect_inherits_allowlist_runtime_env(mock_process, monkeypatch):
    """PATH/HOME/locale must be inherited so npx/uvx can locate their runtime."""
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("HOME", "/home/agent")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    client = MCPClient(timeout=1.0)
    mock_process.stdout.read.return_value = _init_response_bytes()

    with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
        await client.connect(["npx", "server"])
        passed_env = mock_exec.call_args.kwargs["env"]

    assert passed_env["PATH"] == "/usr/local/bin:/usr/bin"
    assert passed_env["HOME"] == "/home/agent"
    assert passed_env["LANG"] == "en_US.UTF-8"


@pytest.mark.asyncio
async def test_mcp_connect_passes_user_env_from_yaml(mock_process, monkeypatch):
    """Per-server env declared in mcp_servers.yaml is layered on top of the base."""
    monkeypatch.setenv("PATH", "/usr/bin")
    client = MCPClient(timeout=1.0)
    mock_process.stdout.read.return_value = _init_response_bytes()

    with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
        await client.connect(["npx", "server"], env={"MY_TOOL_KEY": "tool-secret"})
        passed_env = mock_exec.call_args.kwargs["env"]

    assert passed_env["MY_TOOL_KEY"] == "tool-secret"


@pytest.mark.asyncio
async def test_mcp_connect_filters_secret_user_env(mock_process, monkeypatch):
    """A secret declared in mcp_servers.yaml env is still dropped (denylist wins)."""
    monkeypatch.setenv("PATH", "/usr/bin")
    client = MCPClient(timeout=1.0)
    mock_process.stdout.read.return_value = _init_response_bytes()

    with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
        await client.connect(
            ["npx", "server"],
            env={"CORPCLAW_IPC_SECRET": "should-not-pass", "MY_TOOL_KEY": "ok"},
        )
        passed_env = mock_exec.call_args.kwargs["env"]

    assert "CORPCLAW_IPC_SECRET" not in passed_env
    assert passed_env["MY_TOOL_KEY"] == "ok"


@pytest.mark.asyncio
async def test_mcp_connect_drops_corpclaw_prefixed_vars(mock_process, monkeypatch):
    """Any CORPCLAW_-prefixed var (current or future) is dropped, not just the enumerated ones."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CORPCLAW_PRIVATE_EXTENSIONS", "/secret/overlay")
    monkeypatch.setenv("CORPCLAW_FUTURE_SECRET", "future-leak")

    client = MCPClient(timeout=1.0)
    mock_process.stdout.read.return_value = _init_response_bytes()

    with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
        await client.connect(["npx", "server"])
        passed_env = mock_exec.call_args.kwargs["env"]

    assert "CORPCLAW_PRIVATE_EXTENSIONS" not in passed_env
    assert "CORPCLAW_FUTURE_SECRET" not in passed_env
