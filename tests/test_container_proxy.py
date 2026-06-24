"""Tests for container/proxy.py — IPCToolProxy argument filtering.

Regression coverage for a bug where host-side kwargs injected by ToolRegistry
(``user``, ``run_id``, progress callbacks, ``parent_trajectory_recorder``)
leaked into the IPC payload and crashed ``IPCAuth.sign`` with
``TypeError: Object of type function is not JSON serializable``, breaking
EVERY tool call in container mode.
"""

from __future__ import annotations

from typing import Any

import pytest

from corpclaw_lite.container.proxy import IPCToolProxy
from corpclaw_lite.exceptions import ContainerIPCError
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.users.models import User

# ── Helpers ───────────────────────────────────────────────────────────────────


def _noop1(_a: Any) -> None:  # type: ignore[empty-body]
    pass


def _noop2(_a: Any, _b: Any) -> None:  # type: ignore[empty-body]
    pass


# ── Fixtures ──────────────────────────────────────────────────────────────────


class _StubIPC:
    """Captures the args IPCToolProxy forwards to the container."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def send_tool_call(
        self,
        user_id: int,
        tool_name: str,
        args: dict[str, Any],
    ) -> str:
        self.captured = {
            "user_id": user_id,
            "tool_name": tool_name,
            "args": args,
        }
        return "ok"


class _ReadFileTool(Tool):
    name = "read_file"
    description = "read a file"
    params = [ToolParam(name="path", type="string", description="path")]
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        return ""


@pytest.fixture
def user() -> User:
    return User(id=42, name="Tester", department="engineering")  # type: ignore[call-arg]


@pytest.fixture
def ipc() -> _StubIPC:
    return _StubIPC()


@pytest.fixture
def proxy(ipc: _StubIPC) -> IPCToolProxy:
    return IPCToolProxy.from_tool(_ReadFileTool(), ipc)  # type: ignore[arg-type]


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_host_only_kwargs_are_stripped(proxy: IPCToolProxy, ipc: _StubIPC, user: User) -> None:
    """Callbacks, run_id, recorders must never reach the IPC payload."""
    import asyncio

    async def _go() -> None:
        await proxy.execute(
            path="foo.txt",
            user=user,
            run_id="abc",
            on_subagent_tool_start=_noop2,
            on_subagent_tool_batch_start=_noop2,
            on_subagent_llm_stage=_noop2,
            on_subagent_llm_queue_status=_noop2,
            parent_trajectory_recorder=object(),
        )

    asyncio.run(_go())
    assert ipc.captured["args"] == {"path": "foo.txt"}


def test_only_declared_params_forwarded(proxy: IPCToolProxy, ipc: _StubIPC, user: User) -> None:
    """Undeclared keys (even non-callables) are dropped before IPC."""
    import asyncio

    async def _go() -> None:
        await proxy.execute(
            path="bar.txt",
            user=user,
            unexpected_extra="nope",
        )

    asyncio.run(_go())
    assert ipc.captured["args"] == {"path": "bar.txt"}


def test_missing_user_raises(proxy: IPCToolProxy) -> None:
    """Without a resolvable user we cannot target a container."""
    import asyncio

    async def _go() -> None:
        await proxy.execute(path="baz.txt")

    with pytest.raises(ContainerIPCError):
        asyncio.run(_go())


def test_real_callbacks_do_not_crash_serialization(ipc: _StubIPC, user: User) -> None:
    """End-to-end regression: full host kwargs + a real IPCAuth-backed signer.

    Before the fix, ``IPCAuth.sign`` raised ``TypeError: Object of type function
    is not JSON serializable`` here.
    """
    import asyncio

    from corpclaw_lite.security.ipc_auth import IPCAuth

    p = IPCToolProxy.from_tool(_ReadFileTool(), ipc)  # type: ignore[arg-type]
    auth = IPCAuth(secret="x" * 32)  # bypass env, satisfies min-length

    async def _go() -> None:
        await p.execute(
            path="ok.txt",
            user=user,
            run_id="r1",
            on_subagent_tool_start=_noop2,
            parent_trajectory_recorder=object(),
        )

    asyncio.run(_go())
    # The filtered args must be signable (this is what crashed before).
    payload = {
        "type": "tool_call",
        "tool": ipc.captured["tool_name"],
        "args": ipc.captured["args"],
        "tool_timeout": 25.0,
    }
    signed = auth.sign(payload)  # must not raise
    assert "signature" in signed
