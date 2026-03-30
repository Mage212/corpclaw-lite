# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Container manager — Docker lifecycle for per-user isolated sandboxes.

Architecture:
    - ContainerManager.ensure_running(user_id) → starts/checks the container (detached)
    - ContainerIPC.send_tool_call(user_id, tool, args) → docker exec per-call (stateless)
    - is_docker_available() → used at startup to fail-fast if container.enabled=true

Security:
    - Container is started once per user, stays idle (0 CPU) between calls
    - All tool execution goes through docker exec into the container (never on host)
    - Workspace directory is bind-mounted at /workspace; container cannot see anything else
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from corpclaw_lite.config.settings import ContainerSettings
from corpclaw_lite.container.policies import ContainerPolicies
from corpclaw_lite.security.network_policy import NetworkPolicy

__all__ = [
    "ContainerManager",
    "ContainerManagerError",
]

try:
    import docker.errors  # type: ignore[import-untyped]

    import docker  # type: ignore[import-untyped]
except ImportError:
    docker = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class ContainerManagerError(Exception):
    """Raised when container lifecycle operations fail."""


class ContainerManager:
    """Manages the lifecycle of Docker containers for isolated user execution.

    Each user gets exactly one persistent container (detach=True) that stays
    running between requests. Tool calls are dispatched via `docker exec` (see
    ContainerIPC), so the container does NOT need to be restarted per-call.

    If Docker is not available, raise ContainerManagerError on construction
    (caller should check is_docker_available() first).
    """

    def __init__(
        self,
        settings: ContainerSettings | None = None,
        network_policy: NetworkPolicy | None = None,
        workspace_base: Path | None = None,
    ) -> None:
        self.settings = settings or ContainerSettings()
        self.network_policy = network_policy
        # Per-user workspace root — each user gets workspace_base/user_{id}/
        self._workspace_base = workspace_base or Path(self.settings.workspace_base)
        self._client = docker.from_env() if docker else None  # type: ignore[union-attr]

        # A simple state map for idle tracking
        self._active_containers: dict[int, asyncio.Task[Any]] = {}

    @staticmethod
    def is_docker_available() -> bool:
        """Check if Docker SDK is installed and daemon is reachable.

        Used at startup to fail-fast when container.enabled=true.
        """
        if docker is None:
            return False
        try:
            client = docker.from_env()
            client.ping()
            return True
        except Exception:
            return False

    def get_user_workspace(self, user_id: int) -> Path:
        """Return the host-side workspace path for a user, creating it if needed."""
        ws = self._workspace_base / f"user_{user_id}"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

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
        """Ensure a container is running for a given user.

        Creates the container if it doesn't exist, restarts if stopped.
        Idempotent: safe to call on every incoming message.

        Returns:
            The container name (e.g. "corpclaw_agent_123").

        Raises:
            ContainerManagerError: If Docker is unavailable or creation fails.
        """
        if not self.is_available() or self._client is None:
            raise ContainerManagerError("Docker is not available.")

        name = f"corpclaw_agent_{user_id}"
        workspace = self.get_user_workspace(user_id)

        try:
            # Check if already running
            container = self._client.containers.get(name)
            if container.status != "running":
                logger.info("Container %s was stopped, restarting...", name)
                container.restart()
            else:
                logger.debug("Container %s already running.", name)
            return name
        except Exception as _e:
            # docker.errors.NotFound → container doesn't exist yet, fall through
            if "404" not in str(_e) and "Not Found" not in str(_e):
                raise ContainerManagerError(f"Error checking container: {_e}") from _e

        # Container doesn't exist — create it
        logger.info("Creating new container %s for user %s", name, user_id)
        args = ContainerPolicies.build_docker_args(
            user_id=user_id,
            settings=self.settings,
            network_policy=self.network_policy,
            workspace_dir=str(workspace.resolve()),
        )

        try:
            self._client.containers.run(**args)
            logger.info("Container %s started.", name)
            return name
        except Exception as e:
            logger.error("Failed to create container for user %d: %s", user_id, e)
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
        except Exception as e:
            if "404" not in str(e) and "Not Found" not in str(e):
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
        idle_seconds = self.settings.idle_timeout_seconds

        for container in containers:
            try:
                status: str = container.status
                # Always remove exited/dead containers
                if status in ("exited", "dead"):
                    container.remove(v=True, force=True)
                    logger.info("Pruned exited container %s", container.name)
                    removed += 1
                    continue

                # For running containers, check age as a proxy for idle time
                if status == "running":
                    attrs: dict[str, Any] = container.attrs or {}
                    state = attrs.get("State", {})
                    started_at_str: str = state.get("StartedAt", "")
                    if started_at_str:
                        started_at_str = started_at_str.replace("Z", "+00:00")
                        if "." in started_at_str:
                            base, frac_tz = started_at_str.split(".", 1)
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
