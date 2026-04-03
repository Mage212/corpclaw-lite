"""Group C — Docker container lifecycle integration tests.

These tests require:
  1. Docker daemon running
  2. Image corpclaw-agent-base:latest built
  3. CORPCLAW_IPC_SECRET set in environment

All tests are marked @pytest.mark.docker_required and are
automatically skipped when Docker is unavailable.

Run:
    uv run pytest tests/debug/test_C_container.py -v -s

Teardown:
    All containers started during the test session are stopped
    in the module-scoped fixture teardown.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Generator

import pytest

from corpclaw_lite.container.manager import ContainerManager
from corpclaw_lite.users.models import User

# All tests in this module require Docker
pytestmark = [pytest.mark.integration, pytest.mark.docker_required]

_DEBUG_USER_ID = 9001  # unique ID so we don't clash with other tests


# ---------------------------------------------------------------------------
# Module-scoped fixture: agent stack WITH container isolation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def agent_stack_with_container(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[tuple, None, None]:
    """Build agent stack with container.enabled=true.

    Uses a dedicated tmp workspace so the container's bind-mount
    doesn't interfere with other tests.
    """
    from corpclaw_lite.agent.factory import build_agent_stack

    if not ContainerManager.is_docker_available():
        pytest.skip("Docker daemon not available — skipping Group C")

    workspace = tmp_path_factory.mktemp("c_workspaces")

    # Override container settings via env
    prev_enabled = os.environ.get("CORPCLAW_CONTAINER_ENABLED")
    prev_workspace = os.environ.get("CORPCLAW_CONTAINER_WORKSPACE_BASE")
    os.environ["CORPCLAW_CONTAINER_ENABLED"] = "true"
    os.environ["CORPCLAW_CONTAINER_WORKSPACE_BASE"] = str(workspace)
    os.environ.setdefault("CORPCLAW_IPC_SECRET", "debug-integration-secret")

    loop = None
    container_mgr = None
    try:
        loop, _user_mgr, registry, _mcp, container_mgr = build_agent_stack()
        yield loop, registry, container_mgr, workspace
    except RuntimeError as e:
        pytest.skip(f"Cannot build containerised stack: {e}")
    finally:
        # Restore env
        if prev_enabled is None:
            os.environ.pop("CORPCLAW_CONTAINER_ENABLED", None)
        else:
            os.environ["CORPCLAW_CONTAINER_ENABLED"] = prev_enabled
        if prev_workspace is None:
            os.environ.pop("CORPCLAW_CONTAINER_WORKSPACE_BASE", None)
        else:
            os.environ["CORPCLAW_CONTAINER_WORKSPACE_BASE"] = prev_workspace

        # Teardown: stop test container
        if container_mgr is not None:
            container_mgr.stop(user_id=_DEBUG_USER_ID)


@pytest.fixture(scope="module")
def c_user() -> User:
    return User(id=_DEBUG_USER_ID, name="ContainerDebugUser", department="debug")


# ---------------------------------------------------------------------------
# C1 — Container is started on first message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C1_container_starts_on_first_message(
    agent_stack_with_container: tuple,
    c_user: User,
) -> None:
    """ContainerManager.ensure_running() is called and container appears in docker ps."""
    loop, _, container_mgr, _ = agent_stack_with_container

    # Trigger container start by ensuring it's running
    name = container_mgr.ensure_running(user_id=c_user.id)
    assert name == f"corpclaw_agent_{c_user.id}", (
        f"Expected container name 'corpclaw_agent_{c_user.id}', got '{name}'"
    )

    # Verify it appears in docker list
    active = await container_mgr.list_active()
    assert any(f"corpclaw_agent_{c_user.id}" in n for n in active), (
        f"Container not found in active list: {active}"
    )
    print(f"\n[C1] Active containers: {active}")


# ---------------------------------------------------------------------------
# C2 — Container is reused on second ensure_running call
# ---------------------------------------------------------------------------


def test_C2_container_reused_on_second_call(
    agent_stack_with_container: tuple,
    c_user: User,
) -> None:
    """ensure_running() called twice does not create a second container."""
    _, _, container_mgr, _ = agent_stack_with_container

    name1 = container_mgr.ensure_running(user_id=c_user.id)
    name2 = container_mgr.ensure_running(user_id=c_user.id)

    assert name1 == name2, "Container name changed between calls — new container was created"
    print(f"\n[C2] Container stable: {name1}")


# ---------------------------------------------------------------------------
# C3 — IPC: write_file via container creates file in workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C3_ipc_write_file_creates_workspace_file(
    agent_stack_with_container: tuple,
    c_user: User,
) -> None:
    """write_file routed through IPC creates the file in the host-side workspace."""
    loop, registry, container_mgr, workspace = agent_stack_with_container

    # Ensure container is running before the IPC call
    container_mgr.ensure_running(user_id=c_user.id)

    # Execute write_file via IPC (IPCToolProxy wraps the real tool)
    ipc_tool = registry.get("write_file")
    assert ipc_tool is not None, "write_file not registered (IPC proxy expected)"

    result = await registry.execute(
        "write_file",
        {"path": "ipc_test.txt", "content": "IPC_WRITE_C03"},
        user=c_user,
    )

    assert "error" not in result.lower(), f"write_file via IPC failed: {result}"

    # File must exist in the host workspace
    expected = workspace / f"user_{c_user.id}" / "ipc_test.txt"
    assert expected.exists(), (
        f"File not found at host path {expected}.\nIPC result: {result}"
    )
    assert "IPC_WRITE_C03" in expected.read_text(encoding="utf-8")
    print(f"\n[C3] IPC write result: {result}")


# ---------------------------------------------------------------------------
# C4 — Workspace path traversal blocked (container-side enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C4_workspace_path_traversal_blocked(
    agent_stack_with_container: tuple,
    c_user: User,
) -> None:
    """Attempting to read outside /workspace via IPC must be blocked."""
    _, registry, container_mgr, _ = agent_stack_with_container

    container_mgr.ensure_running(user_id=c_user.id)

    result = await registry.execute(
        "read_file",
        {"path": "../../etc/passwd"},
        user=c_user,
    )

    assert any(kw in result.lower() for kw in ("denied", "access", "outside", "error")), (
        f"Expected path-traversal denial, got:\n{result}"
    )
    print(f"\n[C4] Traversal result: {result[:200]}")


# ---------------------------------------------------------------------------
# C5 — Idle container is pruned when timeout expires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C5_prune_removes_idle_container(
    agent_stack_with_container: tuple,
    c_user: User,
) -> None:
    """prune_idle() removes the container when idle_timeout_seconds=0."""
    _, _, container_mgr, _ = agent_stack_with_container

    # Ensure container exists
    container_mgr.ensure_running(user_id=c_user.id)

    # Set timeout to 0 so ANY container is considered idle
    original_timeout = container_mgr.settings.idle_timeout_seconds
    container_mgr.settings.idle_timeout_seconds = 0

    try:
        removed = await container_mgr.prune_idle()
    finally:
        container_mgr.settings.idle_timeout_seconds = original_timeout

    assert removed >= 1, (
        f"Expected at least 1 container pruned with timeout=0, got {removed}"
    )
    print(f"\n[C5] Pruned {removed} container(s)")


# ---------------------------------------------------------------------------
# C6 — IPC with wrong HMAC is rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C6_ipc_bad_hmac_rejected(
    agent_stack_with_container: tuple,
    c_user: User,
) -> None:
    """ContainerIPC with a bad HMAC secret must fail signature verification."""
    from corpclaw_lite.container.ipc import ContainerIPC
    from corpclaw_lite.security.ipc_auth import IPCAuth

    _, _, container_mgr, _ = agent_stack_with_container
    container_mgr.ensure_running(user_id=c_user.id)

    # Create IPC with a wrong secret
    bad_auth = IPCAuth.__new__(IPCAuth)
    bad_auth._secret = b"totally-wrong-secret-xyz"  # type: ignore[attr-defined]
    bad_ipc = ContainerIPC(auth=bad_auth, timeout_seconds=10.0)

    result = await bad_ipc.send_tool_call(
        user_id=c_user.id,
        tool_name="read_file",
        args={"path": "test.txt"},
    )

    assert any(kw in result.lower() for kw in ("security", "verification", "invalid", "error", "hmac")), (
        f"Expected HMAC verification failure, got:\n{result}"
    )
    print(f"\n[C6] Bad HMAC result: {result[:200]}")
