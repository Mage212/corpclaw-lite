# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
import asyncio
import logging
from typing import Any

from corpclaw_lite.config.settings import ContainerSettings
from corpclaw_lite.container.policies import ContainerPolicies
from corpclaw_lite.security.network_policy import NetworkPolicy

__all__ = [
    "ContainerManager",
    "ContainerManagerError",
]

try:
    import docker
except ImportError:
    docker = None

logger = logging.getLogger(__name__)


class ContainerManagerError(Exception):
    pass


class ContainerManager:
    """Manages the lifecycle of Docker containers for isolated user execution."""

    def __init__(
        self, settings: ContainerSettings | None = None, network_policy: NetworkPolicy | None = None
    ):
        self.settings = settings or ContainerSettings()
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
            logger.info("Container %s is already running.", name)
            return name
        except docker.errors.NotFound:  # type: ignore
            pass

        # Needs creation
        logger.info("Creating new container %s for user %s", name, user_id)

        args = ContainerPolicies.build_docker_args(
            user_id=user_id, settings=self.settings, network_policy=self.network_policy
        )

        try:
            container = self._client.containers.run(**args)
            return name
        except Exception as e:
            logger.error("Failed to create container: %s", e)
            raise ContainerManagerError(f"Could not start container: {e}") from e

    def stop(self, user_id: int) -> None:
        """Stop and remove a user's container immediately."""
        if not self._client:
            return

        name = f"corpclaw_agent_{user_id}"
        try:
            container = self._client.containers.get(name)
            container.stop(timeout=2)
            container.remove(v=True, force=True)
            logger.info("Stopped and removed container %s", name)
        except docker.errors.NotFound:  # type: ignore
            pass
        except Exception as e:
            logger.error("Failed to stop container %s: %s", name, e)

    async def list_active(self) -> list[str]:
        """Return names of all active corpclaw agent containers."""
        if not self._client:
            return []
        try:
            containers = self._client.containers.list(filters={"name": "corpclaw_agent_"})
            return [str(c.name) for c in containers]
        except Exception:
            return []

    async def prune_idle(self) -> int:
        """Prune containers that have been idle past settings.idle_timeout_seconds.
        Returns the number of containers removed.
        """
        if not self._client:
            return 0

        removed = 0
        try:
            containers = self._client.containers.list(all=True, filters={"name": "corpclaw_agent_"})
        except Exception as exc:
            logger.error("Failed to list containers for pruning: %s", exc)
            return 0

        import datetime as dt

        now = dt.datetime.now(tz=dt.UTC)
        idle_seconds = getattr(self.settings, "idle_timeout_seconds", 600)

        for container in containers:
            try:
                status: str = container.status  # type: ignore[assignment]
                # Always remove exited/dead containers
                if status in ("exited", "dead"):
                    container.remove(v=True, force=True)
                    logger.info("Pruned exited container %s", container.name)
                    removed += 1
                    continue

                # For running containers, check if they've been idle
                if status == "running":
                    attrs: dict[str, Any] = container.attrs or {}  # type: ignore[assignment]
                    state = attrs.get("State", {})
                    started_at_str: str = state.get("StartedAt", "")
                    if started_at_str:
                        # Parse Docker ISO timestamp
                        started_at_str = started_at_str.replace("Z", "+00:00")
                        # Truncate nanosecond precision to microseconds
                        if "." in started_at_str:
                            base, frac_tz = started_at_str.split(".", 1)
                            # Separate fractional seconds from timezone
                            frac = ""
                            tz_part = ""
                            for i, ch in enumerate(frac_tz):
                                if ch in ("+", "-"):
                                    frac = frac_tz[:i]
                                    tz_part = frac_tz[i:]
                                    break
                            else:
                                frac = frac_tz
                            started_at_str = f"{base}.{frac[:6]}{tz_part}"

                        started_at = dt.datetime.fromisoformat(started_at_str)
                        age = (now - started_at).total_seconds()
                        if age > idle_seconds:
                            container.stop(timeout=2)
                            container.remove(v=True, force=True)
                            logger.info(
                                "Pruned idle container %s (age=%ds)", container.name, int(age)
                            )
                            removed += 1
            except Exception as exc:
                logger.debug("Error pruning container %s: %s", container.name, exc)

        return removed
