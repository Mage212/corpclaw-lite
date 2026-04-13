"""Tests for container/ipc.py — ContainerIPC mock-based unit tests.

All tests mock asyncio.create_subprocess_exec so Docker is NOT required.
IPCAuth uses a test secret set via monkeypatch.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from corpclaw_lite.container.ipc import ContainerIPC
from corpclaw_lite.exceptions import ContainerIPCError

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _ipc_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure CORPCLAW_IPC_SECRET is set for all tests."""
    monkeypatch.setenv("CORPCLAW_IPC_SECRET", "test-secret-key-for-ci")


@pytest.fixture
def auth():
    from corpclaw_lite.security.ipc_auth import IPCAuth

    return IPCAuth()


@pytest.fixture
def ipc(auth):
    return ContainerIPC(auth=auth, timeout_seconds=5.0)


def _make_signed_response(auth, payload: dict) -> bytes:
    """Helper: sign a payload and return it as a JSON-encoded byte string."""
    signed = auth.sign(payload)
    return (json.dumps(signed) + "\n").encode("utf-8")


def _mock_process(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Create an AsyncMock process with given outputs."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ── Test 1: from_env ──────────────────────────────────────────────────────────


def test_from_env() -> None:
    ipc = ContainerIPC.from_env(timeout_seconds=10.0)
    assert isinstance(ipc, ContainerIPC)
    assert ipc.timeout == 10.0


# ── Test 2: container_name ────────────────────────────────────────────────────


def test_container_name() -> None:
    assert ContainerIPC.container_name(42) == "corpclaw_agent_42"
    assert ContainerIPC.container_name(0) == "corpclaw_agent_0"


# ── Test 3: send_tool_call success ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tool_call_success(ipc, auth) -> None:
    response_payload = {"status": "success", "result": "file contents here"}
    stdout = _make_signed_response(auth, response_payload)
    proc = _mock_process(stdout=stdout, returncode=0)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await ipc.send_tool_call(user_id=1, tool_name="read_file", args={"path": "/a"})

    assert result == "file contents here"


# ── Test 4: send_tool_call error status ───────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tool_call_error_status(ipc, auth) -> None:
    response_payload = {"status": "error", "error": "permission denied"}
    stdout = _make_signed_response(auth, response_payload)
    proc = _mock_process(stdout=stdout, returncode=0)

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(ContainerIPCError) as exc_info,
    ):
        await ipc.send_tool_call(user_id=1, tool_name="write_file", args={})

    assert "permission denied" in str(exc_info.value)


# ── Test 5: nonzero exit code ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tool_call_nonzero_exit(ipc) -> None:
    proc = _mock_process(stdout=b"", stderr=b"No such container", returncode=1)

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(ContainerIPCError) as exc_info,
    ):
        await ipc.send_tool_call(user_id=1, tool_name="exec_script", args={})

    assert "No such container" in str(exc_info.value)


# ── Test 6: invalid JSON response ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tool_call_invalid_json(ipc) -> None:
    proc = _mock_process(stdout=b"not valid json {{{", returncode=0)

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(ContainerIPCError) as exc_info,
    ):
        await ipc.send_tool_call(user_id=1, tool_name="read_file", args={})

    assert "Invalid JSON" in str(exc_info.value)


# ── Test 7: verify raises (bad signature) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tool_call_verify_fails(ipc) -> None:
    # Valid JSON but with bad/missing signature
    bad_response = json.dumps(
        {
            "signature": "bad",
            "nonce": "bad",
            "timestamp": "0",
            "payload": {"status": "success", "result": "hacked"},
        }
    ).encode("utf-8")
    proc = _mock_process(stdout=bad_response, returncode=0)

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(ContainerIPCError) as exc_info,
    ):
        await ipc.send_tool_call(user_id=1, tool_name="read_file", args={})

    assert "Security verification failed" in str(exc_info.value)


# ── Test 8: timeout ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tool_call_timeout(ipc) -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=TimeoutError())
    proc.kill = lambda: None
    proc.wait = AsyncMock(return_value=0)

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(ContainerIPCError) as exc_info,
    ):
        await ipc.send_tool_call(user_id=1, tool_name="slow_tool", args={})

    assert "timed out" in str(exc_info.value)


# ── Test 9: generic OSError ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tool_call_generic_error(ipc) -> None:
    with (
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("docker not found"),
        ),
        pytest.raises(ContainerIPCError) as exc_info,
    ):
        await ipc.send_tool_call(user_id=1, tool_name="read_file", args={})

    assert "docker not found" in str(exc_info.value)


# ── Test 10: get_last_used ────────────────────────────────────────────────────


def test_get_last_used_none_initially(ipc) -> None:
    assert ipc.get_last_used(999) is None


@pytest.mark.asyncio
async def test_get_last_used_after_call(ipc, auth) -> None:
    response_payload = {"status": "success", "result": "ok"}
    stdout = _make_signed_response(auth, response_payload)
    proc = _mock_process(stdout=stdout, returncode=0)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await ipc.send_tool_call(user_id=42, tool_name="read_file", args={})

    ts = ipc.get_last_used(42)
    assert ts is not None
    assert isinstance(ts, float)


# ── Test 11: last_used updates on each call ───────────────────────────────────


@pytest.mark.asyncio
async def test_last_used_updates_on_call(ipc, auth) -> None:
    response_payload = {"status": "success", "result": "ok"}
    stdout = _make_signed_response(auth, response_payload)

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_mock_process(stdout=stdout, returncode=0),
    ):
        await ipc.send_tool_call(user_id=7, tool_name="t1", args={})
        ts1 = ipc.get_last_used(7)

    # Small delay to ensure monotonic moves forward
    await asyncio.sleep(0.01)

    stdout2 = _make_signed_response(auth, response_payload)
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_mock_process(stdout=stdout2, returncode=0),
    ):
        await ipc.send_tool_call(user_id=7, tool_name="t2", args={})
        ts2 = ipc.get_last_used(7)

    assert ts1 is not None
    assert ts2 is not None
    assert ts2 > ts1


# ── Test 12: tool_timeout derivation ──────────────────────────────────────────


def test_tool_timeout_derived_from_ipc_timeout(auth) -> None:
    """tool_timeout = ipc_timeout - 5s overhead."""
    ipc_30 = ContainerIPC(auth=auth, timeout_seconds=30.0)
    assert ipc_30.tool_timeout == 25.0

    ipc_120 = ContainerIPC(auth=auth, timeout_seconds=120.0)
    assert ipc_120.tool_timeout == 115.0


@pytest.mark.asyncio
async def test_tool_timeout_propagated_in_payload(ipc, auth) -> None:
    """send_tool_call must include tool_timeout in the IPC payload."""
    response_payload = {"status": "success", "result": "ok"}
    stdout = _make_signed_response(auth, response_payload)
    proc = _mock_process(stdout=stdout, returncode=0)

    sent_input: bytes = b""

    async def _capture_communicate(input: bytes) -> tuple[bytes, bytes]:
        nonlocal sent_input
        sent_input = input
        return (stdout, b"")

    proc.communicate = _capture_communicate

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        ipc_with_time = ContainerIPC(auth=auth, timeout_seconds=30.0)
        result = await ipc_with_time.send_tool_call(
            user_id=1, tool_name="read_file", args={"path": "/a"}
        )

    assert result == "ok"

    # Decode the signed payload sent to the container
    signed_msg = json.loads(sent_input.decode("utf-8"))
    inner = auth.verify(signed_msg)
    assert inner["tool_timeout"] == 25.0  # 30.0 - 5.0
