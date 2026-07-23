from unittest.mock import MagicMock, patch

import pytest

from corpclaw_lite.config.settings import ContainerSettings
from corpclaw_lite.container.manager import ContainerManager


def test_container_settings_rejects_unknown_key() -> None:
    """S3-16: ContainerSettings is extra=forbid, so a legacy/typo'd key is rejected.

    Supersedes the S2.3 guard: the dead ``max_per_user`` field was removed, and
    ContainerSettings now forbids unknown keys so a typo cannot silently disable
    hardening (e.g. a misspelled strict_capabilities would drop cap_drop).
    Operators carrying a stale overlay must remove the legacy key.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ContainerSettings.model_validate(
            {
                "enabled": True,
                "max_per_user": 1,  # legacy overlay key — now rejected, not ignored
                "max_concurrent_containers": 15,
            }
        )


@pytest.fixture
def mock_docker():
    with patch("corpclaw_lite.container.manager.docker") as m_docker:
        yield m_docker


def test_container_manager_is_available(mock_docker):
    # Mocking docker SDK client
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_docker.from_env.return_value = mock_client

    settings = ContainerSettings()
    manager = ContainerManager(settings)

    assert manager.is_available() is True
    mock_client.ping.assert_called_once()


def test_container_manager_ensure_running_creates_new(mock_docker):
    mock_client = MagicMock()
    # Simulate container not found
    mock_client.containers.get.side_effect = Exception("Not Found")

    # Simulate run success
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    mock_docker.from_env.return_value = mock_client

    settings = ContainerSettings()
    manager = ContainerManager(settings)

    # Mock docker.errors.NotFound since we patch docker module
    mock_docker.errors.NotFound = Exception

    name = manager.ensure_running(user_id=900000042)
    assert name == "corpclaw_agent_900000042"
    mock_client.containers.run.assert_called_once()

    args = mock_client.containers.run.call_args.kwargs
    assert args["image"] == "corpclaw-agent-base:latest"
    assert args["name"] == "corpclaw_agent_900000042"
    assert args["mem_limit"] == "512m"
    # B-064: hardening applied by default (cap_drop + non-root + seccomp)
    assert args["user"] == "agent"
    assert args["cap_drop"] == ["ALL"]
    assert any(s.startswith("seccomp=") for s in args["security_opt"])


def test_container_manager_stop(mock_docker):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.get.return_value = mock_container

    mock_docker.from_env.return_value = mock_client

    settings = ContainerSettings()
    manager = ContainerManager(settings)

    manager.stop(user_id=123)

    mock_client.containers.get.assert_called_once_with("corpclaw_agent_123")
    mock_container.stop.assert_called_once_with(timeout=2)
    mock_container.remove.assert_called_once_with(v=True, force=True)


@pytest.mark.asyncio
async def test_container_list_active(mock_docker):
    mock_client = MagicMock()
    c1 = MagicMock()
    c1.name = "corpclaw_agent_1"
    c2 = MagicMock()
    c2.name = "corpclaw_agent_2"
    mock_client.containers.list.return_value = [c1, c2]
    mock_docker.from_env.return_value = mock_client

    manager = ContainerManager(ContainerSettings())
    names = await manager.list_active()
    assert names == ["corpclaw_agent_1", "corpclaw_agent_2"]


@pytest.mark.asyncio
async def test_container_list_active_no_docker():
    """Without docker SDK, list_active returns empty."""
    manager = ContainerManager.__new__(ContainerManager)
    manager._client = None
    manager.settings = ContainerSettings()
    manager.network_policy = None
    manager._active_containers = {}
    names = await manager.list_active()
    assert names == []


@pytest.mark.asyncio
async def test_container_prune_idle(mock_docker):
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    manager = ContainerManager(ContainerSettings())
    removed = await manager.prune_idle()
    assert removed == 0


def test_container_not_available_no_docker():
    """Without docker SDK, is_available returns False."""
    manager = ContainerManager.__new__(ContainerManager)
    manager._client = None
    manager.settings = ContainerSettings()
    manager.network_policy = None
    manager._active_containers = {}
    assert manager.is_available() is False


def test_docker_args_with_network_policy():
    """build_docker_args must apply deny-all network_mode from NetworkPolicy."""
    from corpclaw_lite.container.policies import build_docker_args
    from corpclaw_lite.security.network_policy import NetworkPolicy

    settings = ContainerSettings()
    policy = NetworkPolicy()

    args = build_docker_args(user_id=1, settings=settings, network_policy=policy)

    env = args["environment"]
    assert isinstance(env, dict)
    assert env["CORPCLAW_USER_ID"] == "1"
    # IPC secret must not live in long-lived container create env
    assert "CORPCLAW_IPC_SECRET" not in env
    assert args["network_mode"] == "none"


def test_container_stop_no_docker():
    """stop() with no docker client is a no-op."""
    manager = ContainerManager.__new__(ContainerManager)
    manager._client = None
    manager.settings = ContainerSettings()
    manager.network_policy = None
    manager._active_containers = {}
    manager.stop(user_id=999)  # should not raise


# ── S3-02: seccomp fail-fast + generation labels ─────────────────────────────


def test_build_docker_args_writes_generation_labels():
    """build_docker_args labels containers for policy re-validation on reuse."""
    from corpclaw_lite.container.policies import build_docker_args

    args = build_docker_args(user_id=1, settings=ContainerSettings())
    labels = args.get("labels", {})
    assert labels["corpclaw.image"] == "corpclaw-agent-base:latest"
    assert labels["corpclaw.strict_capabilities"] == "True"
    assert labels["corpclaw.network"] == "default"


def test_build_docker_args_seccomp_missing_fatal_raises(tmp_path, monkeypatch):
    """Missing seccomp profile with seccomp_missing_fatal=True fails fast."""
    from corpclaw_lite.container.policies import (
        ContainerPolicyError,
        build_docker_args,
    )

    settings = ContainerSettings(strict_capabilities=True, seccomp_missing_fatal=True)
    # Point seccomp path at a non-existent file.
    monkeypatch.setattr(
        "corpclaw_lite.container.policies.seccomp_profile_path",
        "definitely-missing-seccomp.json",
        raising=False,
    )
    monkeypatch.setattr("corpclaw_lite.container.policies.PROJECT_ROOT", tmp_path, raising=False)
    with pytest.raises(ContainerPolicyError, match="Seccomp profile not found"):
        build_docker_args(user_id=1, settings=settings)


def test_build_docker_args_seccomp_missing_non_fatal_skips(tmp_path, monkeypatch):
    """Missing seccomp profile with seccomp_missing_fatal=False logs and skips."""
    from corpclaw_lite.container.policies import build_docker_args

    settings = ContainerSettings(strict_capabilities=True, seccomp_missing_fatal=False)
    monkeypatch.setattr("corpclaw_lite.container.policies.PROJECT_ROOT", tmp_path, raising=False)
    args = build_docker_args(user_id=1, settings=settings, seccomp_profile_path="missing.json")
    # No seccomp entry in security_opt, but cap_drop/user still applied.
    assert args["user"] == "agent"
    assert args["cap_drop"] == ["ALL"]
    assert not any(s.startswith("seccomp=") for s in args["security_opt"])
