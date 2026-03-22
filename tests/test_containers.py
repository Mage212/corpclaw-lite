import pytest
from unittest.mock import MagicMock, patch

from corpclaw_lite.container.manager import ContainerManager, ContainerManagerError
from corpclaw_lite.config.settings import ContainerSettings

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
    
    name = manager.ensure_running(user_id=42)
    assert name == "corpclaw_agent_42"
    mock_client.containers.run.assert_called_once()
    
    args = mock_client.containers.run.call_args.kwargs
    assert args["image"] == "corpclaw-agent-base:latest"
    assert args["name"] == "corpclaw_agent_42"
    assert args["mem_limit"] == "512m"


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
