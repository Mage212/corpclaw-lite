"""Tests for AgentWorker inside container."""

from unittest.mock import MagicMock, patch

import pytest

from corpclaw_lite.container.agent_worker import process_request


@pytest.fixture(autouse=True)
def mock_env():
    import os

    os.environ["CORPCLAW_IPC_SECRET"] = "test-secret"
    yield
    del os.environ["CORPCLAW_IPC_SECRET"]


def test_process_request_empty_input():
    with patch("sys.stdin.read", return_value=""), patch("builtins.print") as mock_print:
        process_request()
        mock_print.assert_not_called()


def test_process_request_invalid_json():
    with (
        patch("sys.stdin.read", return_value="invalid"),
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

    with (
        patch("sys.stdin.read", return_value=req),
        patch("builtins.print") as mock_print,
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.verify") as mock_verify,
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.sign") as mock_sign,
        patch("corpclaw_lite.container.agent_worker.registry") as mock_registry,
    ):
        mock_verify.return_value = {"type": "tool_call", "tool": "test_tool", "args": {"a": 1}}
        mock_tool = MagicMock()
        mock_registry.get.return_value = mock_tool

        # Async execution mocked since tool.execute is async
        async def dummy_execute(**kwargs):
            return "tool result"

        mock_tool.execute = dummy_execute

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
        patch("sys.stdin.read", return_value=req),
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
