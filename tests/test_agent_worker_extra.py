"""Tests for AgentWorker inside container."""

from unittest.mock import MagicMock, patch

import pytest

from corpclaw_lite.container.agent_worker import process_request


@pytest.fixture(autouse=True)
def mock_env():
    import os

    os.environ["CORPCLAW_IPC_SECRET"] = "test-secret-at-least-32-chars!!!"
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
    req = '{"payload": "test-value-long-enough-for-secret-length"}'

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
    req = '{"payload": "test-value-long-enough-for-secret-length"}'

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
    """P1-4: Tool execution that exceeds tool_timeout returns a timeout error."""
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
    ):
        # tool_timeout=0.01 sent in payload — tool sleeps 60s, must timeout
        mock_verify.return_value = {
            "type": "tool_call",
            "tool": "slow_tool",
            "args": {},
            "tool_timeout": 0.01,
        }
        mock_sign.return_value = {"signed": "timeout_resp"}

        process_request()

        mock_sign.assert_called_once()
        resp_payload = mock_sign.call_args[0][0]
        assert resp_payload["status"] == "error"
        # asyncio.TimeoutError has no message text — just verify it's an error,
        # not the slow_execute return value
        assert resp_payload.get("result") is None


def test_process_request_reads_secret_from_stdin():
    """S3-03: the IPC secret arrives on stdin (line 1), not in process env/argv.

    The worker constructs IPCAuth with the stdin secret, and never reads
    CORPCLAW_IPC_SECRET from the environment.
    """
    import os

    # Ensure env does NOT carry the secret — worker must get it from stdin only.
    os.environ.pop("CORPCLAW_IPC_SECRET", None)
    secret = "test-secret-at-least-32-chars!!"
    payload = '{"payload": "test"}'

    readline_returns = iter([secret + "\n", payload + "\n"])

    with (
        patch("sys.stdin.readline", side_effect=lambda: next(readline_returns)),
        patch("builtins.print"),
        patch("corpclaw_lite.container.agent_worker.IPCAuth") as mock_auth_cls,
        patch("corpclaw_lite.container.agent_worker.get_registry") as mock_registry,
    ):
        mock_auth = MagicMock()
        mock_auth.verify.return_value = {"type": "tool_call", "tool": "t", "args": {}}
        mock_auth.sign.return_value = {"signed": "r"}
        mock_auth_cls.return_value = mock_auth

        mock_tool = MagicMock()

        async def dummy_execute(**kwargs):
            return "ok"

        mock_tool.execute = dummy_execute
        mock_registry.return_value.get.return_value = mock_tool

        process_request()

        # IPCAuth was constructed with the stdin secret (not None/env) plus the
        # persistent nonce-store path (H-1, code review). The exact path comes
        # from _resolve_nonce_store_path(); assert on secret primarily and
        # confirm nonce_store_path was passed.
        assert mock_auth_cls.call_count == 1
        call_kwargs = mock_auth_cls.call_args.kwargs
        assert call_kwargs["secret"] == secret
        assert "nonce_store_path" in call_kwargs


def test_process_request_uses_tool_timeout_from_payload():
    """Tool timeout is read from IPC payload, not hardcoded."""
    import asyncio

    req = '{"payload": "test"}'

    mock_tool = MagicMock()

    async def slow_execute(**kwargs):
        await asyncio.sleep(5)
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
    ):
        # Very short timeout via payload — must trigger TimeoutError
        mock_verify.return_value = {
            "type": "tool_call",
            "tool": "slow_tool",
            "args": {},
            "tool_timeout": 0.05,
        }
        mock_sign.return_value = {"signed": "timeout_resp"}

        process_request()

        resp_payload = mock_sign.call_args[0][0]
        assert resp_payload["status"] == "error"


def test_process_request_fallback_timeout_when_payload_missing():
    """When payload has no tool_timeout, the default fallback is used."""
    mock_tool = MagicMock()

    async def instant_execute(**kwargs):
        return "ok"

    mock_tool.execute = instant_execute

    mock_registry = MagicMock()
    mock_registry.get.return_value = mock_tool

    req = '{"payload": "test-value-long-enough-for-secret-length"}'
    with (
        patch("sys.stdin.readline", return_value=req),
        patch("builtins.print"),
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.verify") as mock_verify,
        patch("corpclaw_lite.security.ipc_auth.IPCAuth.sign") as mock_sign,
        patch(
            "corpclaw_lite.container.agent_worker.get_registry",
            return_value=mock_registry,
        ),
    ):
        # No tool_timeout in payload — should use _DEFAULT_TOOL_TIMEOUT
        mock_verify.return_value = {
            "type": "tool_call",
            "tool": "fast_tool",
            "args": {},
        }
        mock_sign.return_value = {"signed": "ok_resp"}

        process_request()

        resp_payload = mock_sign.call_args[0][0]
        assert resp_payload["status"] == "success"
        assert resp_payload["result"] == "ok"
