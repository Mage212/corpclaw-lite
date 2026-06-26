"""Tests for container/manager.py — ContainerManager mock-based unit tests.

All tests mock the docker SDK so Docker daemon is NOT required.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from corpclaw_lite.config.settings import ContainerSettings
from corpclaw_lite.container.manager import ContainerManager, ContainerManagerError

# ── Helpers ───────────────────────────────────────────────────────────────────


class FakeNotFound(Exception):
    """Standin for docker.errors.NotFound."""


class FakeAPIError(Exception):
    """Standin for docker.errors.APIError."""


def _make_mock_container(
    name: str = "corpclaw_agent_1",
    status: str = "running",
    started_at: str = "2020-01-01T00:00:00Z",
) -> MagicMock:
    """Create a mock Docker container with common attributes."""
    c = MagicMock()
    c.name = name
    c.status = status
    c.attrs = {"State": {"StartedAt": started_at}}
    return c


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_docker():
    with patch("corpclaw_lite.container.manager.docker") as m:
        m.errors.NotFound = FakeNotFound
        m.errors.APIError = FakeAPIError
        yield m


@pytest.fixture
def manager(mock_docker, tmp_path: Path) -> ContainerManager:
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.containers.list.return_value = []
    mock_docker.from_env.return_value = mock_client
    return ContainerManager(settings=ContainerSettings(), workspace_base=tmp_path)


# ── Tests 12–14: is_docker_available ──────────────────────────────────────────


def test_is_docker_available_true(mock_docker) -> None:
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_docker.from_env.return_value = mock_client
    assert ContainerManager.is_docker_available() is True


def test_is_docker_available_false_no_sdk() -> None:
    with patch("corpclaw_lite.container.manager.docker", None):
        assert ContainerManager.is_docker_available() is False


def test_is_docker_available_false_ping_fail(mock_docker) -> None:
    mock_docker.from_env.side_effect = Exception("daemon not running")
    assert ContainerManager.is_docker_available() is False


# ── Test 15: is_available ping fails ──────────────────────────────────────────


def test_is_available_ping_fails(manager) -> None:
    manager._client.ping.side_effect = Exception("connection refused")
    assert manager.is_available() is False


# ── Test 16: ensure_running docker unavailable ────────────────────────────────


def test_ensure_running_docker_unavailable(mock_docker, tmp_path: Path) -> None:
    mock_docker.from_env.return_value = None
    mgr = ContainerManager.__new__(ContainerManager)
    mgr.settings = ContainerSettings()
    mgr.network_policy = None
    mgr._workspace_base = tmp_path
    mgr._client = None
    mgr._ipc = None
    mgr._active_containers = {}

    with pytest.raises(ContainerManagerError, match="not available"):
        mgr.ensure_running(user_id=1)


# ── Test 17: ensure_running already running ───────────────────────────────────


def test_ensure_running_already_running(manager) -> None:
    container = _make_mock_container(name="corpclaw_agent_10", status="running")
    manager._client.containers.get.return_value = container

    name = manager.ensure_running(user_id=10)
    assert name == "corpclaw_agent_10"
    container.restart.assert_not_called()
    assert manager.managed_container_names() == ["corpclaw_agent_10"]


# ── Test 18: ensure_running stopped → restart ─────────────────────────────────


def test_ensure_running_stopped_restarts(manager) -> None:
    container = _make_mock_container(name="corpclaw_agent_10", status="exited")
    manager._client.containers.get.return_value = container

    name = manager.ensure_running(user_id=10)
    assert name == "corpclaw_agent_10"
    container.restart.assert_called_once()
    assert manager.managed_container_names() == ["corpclaw_agent_10"]


# ── Test 19: ensure_running non-NotFound error ────────────────────────────────


def test_ensure_running_check_error_not_404(manager, mock_docker) -> None:
    manager._client.containers.get.side_effect = RuntimeError("unexpected")

    with pytest.raises(ContainerManagerError, match="Error checking container"):
        manager.ensure_running(user_id=1)


# ── Test 20: ensure_running run fails ─────────────────────────────────────────


def test_ensure_running_run_fails(manager, mock_docker) -> None:
    # Container not found → falls through to create
    manager._client.containers.get.side_effect = FakeNotFound("404")
    # But run() fails
    manager._client.containers.run.side_effect = RuntimeError("image pull failed")

    with pytest.raises(ContainerManagerError, match="Could not start container"):
        manager.ensure_running(user_id=1)
    assert manager.managed_container_names() == []


def test_ensure_running_new_container_is_tracked(manager, mock_docker) -> None:
    manager._client.containers.get.side_effect = FakeNotFound("404")

    name = manager.ensure_running(user_id=11)

    assert name == "corpclaw_agent_11"
    assert manager.managed_container_names() == ["corpclaw_agent_11"]


def test_concurrent_container_creation_is_bounded(mock_docker, tmp_path: Path) -> None:
    """Path B (creation) is globally bounded by max_concurrent_containers.

    A burst of new users all creating containers concurrently must not exceed the
    semaphore cap. The hot-path idempotent check (Path A) is unaffected (separate test).
    """
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.containers.list.return_value = []
    mock_docker.from_env.return_value = mock_client
    # Every user misses (NotFound) → all hit Path B (creation).
    mock_client.containers.get.side_effect = FakeNotFound("404")

    current_concurrent = 0
    max_concurrent = 0
    lock = threading.Lock()

    def _slow_run(**_kwargs: object) -> MagicMock:
        nonlocal current_concurrent, max_concurrent
        with lock:
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
        time.sleep(0.05)  # hold the slot briefly so overlap is observable
        with lock:
            current_concurrent -= 1
        return MagicMock()

    mock_client.containers.run.side_effect = _slow_run

    manager = ContainerManager(
        settings=ContainerSettings(max_concurrent_containers=2), workspace_base=tmp_path
    )

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda uid: manager.ensure_running(user_id=uid), range(6)))

    assert max_concurrent <= 2, f"Semaphore leaked: {max_concurrent} > 2"


def test_ensure_running_path_a_bypasses_semaphore(mock_docker, tmp_path: Path) -> None:
    """Path A (already-running) must NOT acquire the creation semaphore.

    ensure_running fires on every message; serializing the idempotent check on a
    global cap would stall the per-message hot path. Verify the semaphore is never
    touched when the container is already running.
    """
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.containers.list.return_value = []
    mock_docker.from_env.return_value = mock_client
    # Container exists and is running → Path A fast return.
    mock_client.containers.get.return_value = _make_mock_container(status="running")

    manager = ContainerManager(
        settings=ContainerSettings(max_concurrent_containers=1), workspace_base=tmp_path
    )
    # Wrap the semaphore so we can observe acquisitions.
    acquired = []
    real_acquire = manager._create_semaphore.acquire
    real_release = manager._create_semaphore.release

    def _spy_acquire(*a: object, **k: object) -> bool:
        acquired.append(True)
        return real_acquire()

    def _spy_release(*a: object, **k: object) -> None:
        return real_release()

    manager._create_semaphore.acquire = _spy_acquire  # type: ignore[method-assign]
    manager._create_semaphore.release = _spy_release  # type: ignore[method-assign]

    for uid in range(5):
        manager.ensure_running(user_id=uid)

    assert acquired == [], "Path A (already-running) must not touch the semaphore"


def test_stop_managed_stops_only_tracked_containers(manager) -> None:
    manager._managed_user_ids.update({2, 1})
    manager.stop_by_name = MagicMock()

    manager.stop_managed()

    assert manager.stop_by_name.call_count == 2
    assert [call.args[0] for call in manager.stop_by_name.call_args_list] == [
        "corpclaw_agent_1",
        "corpclaw_agent_2",
    ]
    assert manager.managed_container_names() == []


# ── Test 21: stop not found is silent ─────────────────────────────────────────


def test_stop_not_found_silent(manager, mock_docker) -> None:
    manager._client.containers.get.side_effect = FakeNotFound("404")
    # Should not raise
    manager.stop(user_id=999)


# ── Test 22: stop other error logs ────────────────────────────────────────────


def test_stop_other_error_logs(manager) -> None:
    manager._client.containers.get.side_effect = RuntimeError("connection error")
    # Should not raise, but should log
    manager.stop(user_id=999)


# ── Test 23: list_active exception ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_active_exception(manager) -> None:
    manager._client.containers.list.side_effect = RuntimeError("daemon error")
    result = await manager.list_active()
    assert result == []


# ── Tests 24–25: prune_idle edge cases ────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_idle_no_client() -> None:
    mgr = ContainerManager.__new__(ContainerManager)
    mgr._client = None
    mgr.settings = ContainerSettings()
    mgr._ipc = None
    assert await mgr.prune_idle() == 0


@pytest.mark.asyncio
async def test_prune_idle_list_fails(manager) -> None:
    manager._client.containers.list.side_effect = RuntimeError("daemon error")
    assert await manager.prune_idle() == 0


# ── Test 26: prune exited container ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_idle_exited_container(manager) -> None:
    c = _make_mock_container(name="corpclaw_agent_5", status="exited")
    manager._client.containers.list.return_value = [c]

    removed = await manager.prune_idle()
    assert removed == 1
    c.remove.assert_called_once_with(v=True, force=True)


# ── Test 27: prune running + idle ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_idle_running_and_idle(manager) -> None:
    # Container started very long ago → is idle
    c = _make_mock_container(
        name="corpclaw_agent_1",
        status="running",
        started_at="2020-01-01T00:00:00Z",
    )
    manager._client.containers.list.return_value = [c]
    manager.settings.idle_timeout_seconds = 60  # 60 seconds idle timeout

    removed = await manager.prune_idle()
    assert removed == 1
    c.stop.assert_called_once_with(timeout=2)
    c.remove.assert_called_once_with(v=True, force=True)


# ── Test 28: prune running + NOT idle ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_idle_running_not_idle(manager) -> None:
    # Container started just now → not idle
    import datetime as dt

    now_str = dt.datetime.now(tz=dt.UTC).isoformat()
    c = _make_mock_container(
        name="corpclaw_agent_1",
        status="running",
        started_at=now_str,
    )
    manager._client.containers.list.return_value = [c]
    manager.settings.idle_timeout_seconds = 3600  # very generous timeout

    removed = await manager.prune_idle()
    assert removed == 0
    c.stop.assert_not_called()


# ── Test 29: prune with IPC tracking ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_idle_with_ipc_tracking(manager) -> None:
    """When IPC reports recent activity, container should NOT be pruned."""
    mock_ipc = MagicMock()
    # Simulate very recent activity
    mock_ipc.get_last_used.return_value = time.monotonic() - 5  # 5 seconds ago
    manager._ipc = mock_ipc

    c = _make_mock_container(
        name="corpclaw_agent_900000042",
        status="running",
        started_at="2020-01-01T00:00:00Z",  # old container...
    )
    manager._client.containers.list.return_value = [c]
    manager.settings.idle_timeout_seconds = 60

    removed = await manager.prune_idle()
    assert removed == 0  # IPC says it's active
    c.stop.assert_not_called()


# ── Test 30: prune IPC returns None → fallback ────────────────────────────────


@pytest.mark.asyncio
async def test_prune_idle_ipc_no_data_fallback(manager) -> None:
    """When IPC has no data for user, fall back to container age."""
    mock_ipc = MagicMock()
    mock_ipc.get_last_used.return_value = None  # no IPC data
    manager._ipc = mock_ipc

    c = _make_mock_container(
        name="corpclaw_agent_99",
        status="running",
        started_at="2020-01-01T00:00:00Z",  # very old
    )
    manager._client.containers.list.return_value = [c]
    manager.settings.idle_timeout_seconds = 60

    removed = await manager.prune_idle()
    assert removed == 1  # fallback to age → old → prune


# ── Test 31: _get_idle_seconds no StartedAt ───────────────────────────────────


def test_get_idle_seconds_no_started_at(manager) -> None:
    c = MagicMock()
    c.name = "corpclaw_agent_1"
    c.attrs = {"State": {"StartedAt": ""}}

    idle = manager._get_idle_seconds(c)
    assert idle == float("inf")


# ── Test 32: _get_idle_seconds fractional timestamp ───────────────────────────


def test_get_idle_seconds_fractional_ts(manager) -> None:
    # Docker sometimes returns nanosecond-precision timestamps
    c = MagicMock()
    c.name = "corpclaw_agent_1"
    c.attrs = {"State": {"StartedAt": "2020-01-01T00:00:00.123456789+00:00"}}

    idle = manager._get_idle_seconds(c)
    assert idle > 0
    assert isinstance(idle, float)


# ── Test 33: _get_idle_seconds no fractional part ─────────────────────────────


def test_get_idle_seconds_no_fraction(manager) -> None:
    c = MagicMock()
    c.name = "corpclaw_agent_1"
    c.attrs = {"State": {"StartedAt": "2020-01-01T00:00:00Z"}}

    idle = manager._get_idle_seconds(c)
    assert idle > 0
    assert isinstance(idle, float)


# ── Test: prune with container error ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_idle_container_error(manager) -> None:
    """If one container fails during pruning, it is skipped, not a crash."""
    c = _make_mock_container(name="corpclaw_agent_1", status="exited")
    c.remove.side_effect = RuntimeError("busy")
    manager._client.containers.list.return_value = [c]

    removed = await manager.prune_idle()
    assert removed == 0  # error → skipped, no crash


# ── Test: _get_idle_seconds IPC ValueError ────────────────────────────────────


def test_get_idle_seconds_ipc_bad_name(manager) -> None:
    """Container with unparseable name falls back to Docker attrs."""
    mock_ipc = MagicMock()
    mock_ipc.get_last_used.return_value = None
    manager._ipc = mock_ipc

    c = MagicMock()
    c.name = "not_a_valid_container"
    c.attrs = {"State": {"StartedAt": "2020-01-01T00:00:00Z"}}

    idle = manager._get_idle_seconds(c)
    assert idle > 0


# ── Test: dead status container pruned ────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_idle_dead_container(manager) -> None:
    c = _make_mock_container(name="corpclaw_agent_1", status="dead")
    manager._client.containers.list.return_value = [c]

    removed = await manager.prune_idle()
    assert removed == 1
    c.remove.assert_called_once_with(v=True, force=True)
