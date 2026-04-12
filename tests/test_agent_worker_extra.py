"""Tests for AgentWorker inside container."""

from unittest.mock import MagicMock, patch

import pytest

from corpclaw_lite.container.agent_worker import process_request


@pytest.fixture(autouse=True)
def mock_env():
    import os

    os.environ["CORPCLAW_IPC_SECRET"] = "test-secret"
    yield
    os.environ.pop("CORPCLAW_IPC_SECRET", None)


def test_process_request_empty_input():
    # Process reads via readline() — patch that
    with patch("sys.stdin.readline", return_value=""), patch("builtins.print") as mock_print:
        process_request()
        mock_print.assert_not_called()


def test_process_request_invalid_json():
    with (
        patch("sys.stdin.readline", return_value="invalid"),
        patch("builtins.print") as mock_print,
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.sign") as mock_sign,
    ):
        mock_sign.return_value = {"signed": True}
        process_request()

        mock_sign.assert_called_once()
        args = mock_sign.call_args[0][0]
        assert args["status"] == "error"
        assert "Expecting value" in args["error"]

        mock_print.assert_called_once_with('{"signed": true}')


def test_process_request_success():
    req = '{"payload": "test"}'

    mock_tool = MagicMock()

    async def dummy_execute(**kwargs):
        return "tool result"

    mock_tool.execute = dummy_execute

    mock_registry = MagicMock()
    mock_registry.get.return_value = mock_tool

    with (
        patch("sys.stdin.readline", return_value=req),
        patch("builtins.print") as mock_print,
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.verify") as mock_verify,
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.sign") as mock_sign,
        # Patch the get_registry() function so it returns our mock
        patch(
            "corpclaw_lite.container.agent_worker.get_registry",
            return_value=mock_registry,
        ),
    ):
        mock_verify.return_value = {"type": "tool_call", "tool": "test_tool", "args": {"a": 1}}
        mock_sign.return_value = {"signed": "response"}

        process_request()

        mock_sign.assert_called_once()
        resp_payload = mock_sign.call_args[0][0]
        assert resp_payload["status"] == "success"
        assert resp_payload["result"] == "tool result"

        mock_print.assert_called_once_with('{"signed": "response"}')


def test_process_request_auth_failure():
    req = '{"payload": "test"}'

    with (
        patch("sys.stdin.readline", return_value=req),
        patch("builtins.print"),
        patch(
            "corpclaw_lite.security.ipc_auth.IPCAuth.verify", side_effect=ValueError("Auth failed")
        ),
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.sign") as mock_sign,
    ):
        mock_sign.return_value = {"signed": "err"}

        process_request()

        mock_sign.assert_called_once()
        resp_payload = mock_sign.call_args[0][0]
        assert resp_payload["status"] == "error"
        assert resp_payload["error"] == "Auth failed"


def test_process_request_tool_timeout():
    """P1-4: Tool execution that exceeds _TOOL_TIMEOUT returns a timeout error."""
    import asyncio

    req = '{"payload": "test"}'

    mock_tool = MagicMock()

    async def slow_execute(**kwargs):
        await asyncio.sleep(60)
        return "should not reach"

    mock_tool.execute = slow_execute

    mock_registry = MagicMock()
    mock_registry.get.return_value = mock_tool

    with (
        patch("sys.stdin.readline", return_value=req),
        patch("builtins.print"),
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.verify") as mock_verify,
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.sign") as mock_sign,
        patch(
            "corpclaw_lite.container.agent_worker.get_registry",
            return_value=mock_registry,
        ),
        # Use a very short timeout for the test
        patch("corpclaw_lite.container.agent_worker._TOOL_TIMEOUT", 0.01),
    ):
        mock_verify.return_value = {"type": "tool_call", "tool": "slow_tool", "args": {}}
        mock_sign.return_value = {"signed": "timeout_resp"}

        process_request()

        mock_sign.assert_called_once()
        resp_payload = mock_sign.call_args[0][0]
        assert resp_payload["status"] == "error"


def test_process_request_clears_ipc_secret():
    """P0-1: IPC secret is removed from environment after IPCAuth initialization."""
    import os

    os.environ["CORPCLAW_IPC_SECRET"] = "test-secret"
    req = '{"payload": "test"}'

    with (
        patch("sys.stdin.readline", return_value=req),
        patch("builtins.print"),
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.verify") as mock_verify,
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.sign") as mock_sign,
    ):
        mock_verify.return_value = {"type": "tool_call", "tool": "t", "args": {}}
        mock_sign.return_value = {"signed": "r"}

        process_request()

        # The env var should have been popped during process_request
        assert "CORPCLAW_IPC_SECRET" not in os.environ
