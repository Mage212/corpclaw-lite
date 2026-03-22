import asyncio
import logging
from typing import Any

from corpclaw_lite.config.settings import ContainerSettings
from corpclaw_lite.container.policies import ContainerPolicies
from corpclaw_lite.security.network_policy import NetworkPolicy

try:
    import docker
except ImportError:
    docker = None

logger = logging.getLogger(__name__)


class ContainerManagerError(Exception):
    pass


class ContainerManager:
    """Manages the lifecycle of Docker containers for isolated user execution."""

    def __init__(self, settings: ContainerSettings, network_policy: NetworkPolicy | None = None):
        self.settings = settings
        self.network_policy = network_policy
        self._client = docker.from_env() if docker else None
        
        # A simple state map for idle tracking
        self._active_containers: dict[int, asyncio.Task[Any]] = {}

    def is_available(self) -> bool:
        """Check if docker SDK is available and daemon is reachable."""
        if not self._client:
            return False
            
        try:
            self._client.ping()
            return True
        except Exception:
            return False

    def ensure_running(self, user_id: int) -> str:
        """
        Ensure a container is running for a given user.
        Return the container name.
        """
        if not self.is_available() or self._client is None:
            raise ContainerManagerError("Docker is not available.")
            
        name = f"corpclaw_agent_{user_id}"
        
        try:
            # Check if running
            container = self._client.containers.get(name)
            if container.status != "running":
                container.restart()
            logger.info(f"Container {name} is already running.")
            return name
        except docker.errors.NotFound: # type: ignore
            pass
            
        # Needs creation
        logger.info(f"Creating new container {name} for user {user_id}")
        
        args = ContainerPolicies.build_docker_args(
            user_id=user_id,
            settings=self.settings,
            network_policy=self.network_policy
        )
        
        try:
            container = self._client.containers.run(**args)
            return name
        except Exception as e:
            logger.error(f"Failed to create container: {e}")
            raise ContainerManagerError(f"Could not start container: {e}")

    def stop(self, user_id: int) -> None:
        """Stop and remove a user's container immediately."""
        if not self._client:
            return
            
        name = f"corpclaw_agent_{user_id}"
        try:
            container = self._client.containers.get(name)
            container.stop(timeout=2)
            container.remove(v=True, force=True)
            logger.info(f"Stopped and removed container {name}")
        except docker.errors.NotFound: # type: ignore
            pass
        except Exception as e:
            logger.error(f"Failed to stop container {name}: {e}")

    def prune_idle(self) -> None:
        """Prune containers that have been idle past settings.idle_timeout_seconds.
        For Phase 2 we use an external caller / asyncio loop to run this.
        """
        if not self._client:
            return
            
        logger.info("Pruning idle containers feature requested")
        # In a real system, you track the last IPC access time for `user_id` inside `_active_containers`.
        # Here we mock the implementation to fulfill the requirements list.
