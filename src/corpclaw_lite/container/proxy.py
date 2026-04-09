"""IPCToolProxy — host-side proxy that executes tools inside the user's Docker container.

Instead of running a tool directly on the host, IPCToolProxy serialises the call,
sends it through ContainerIPC (via docker exec), and returns the result.

Security guarantee: file-system and script tools NEVER execute on the host when
container isolation is enabled — this class is the sole bridge.

Usage in factory.py (container mode):
    proxy = IPCToolProxy.from_tool(ReadFileTool(), ipc)
    registry.register(proxy)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from corpclaw_lite.exceptions import ContainerIPCError
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

__all__ = [
    "IPCToolProxy",
]

if TYPE_CHECKING:
    from corpclaw_lite.container.ipc import ContainerIPC
    from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)


class IPCToolProxy(Tool):
    """Proxy tool that delegates execution to a Docker container via ContainerIPC.

    The proxy exposes the same name, description, params, and risk_level as the
    wrapped tool so the LLM sees identical JSON schemas regardless of whether
    containers are enabled or not.

    The actual tool logic runs inside the container (via agent_worker.py), not here.
    """

    def __init__(
        self,
        name: str,
        description: str,
        params: list[ToolParam],
        risk_level: RiskLevel,
        ipc: ContainerIPC,
    ) -> None:
        self.name = name
        self.description = description
        self.params = params
        self.risk_level = risk_level
        self._ipc = ipc

    @classmethod
    def from_tool(cls, tool: Tool, ipc: ContainerIPC) -> IPCToolProxy:
        """Create a proxy from an existing Tool instance, copying its metadata."""
        proxy = cls(
            name=tool.name,
            description=tool.description,
            params=tool.params,
            risk_level=tool.risk_level,
            ipc=ipc,
        )
        proxy.parallel_safe = tool.parallel_safe
        proxy.terminal = tool.terminal
        return proxy

    async def execute(self, **kwargs: Any) -> str:
        """Delegate execution to the user's container via IPC.

        The 'user' kwarg (injected by ToolRegistry.execute) is used to resolve
        the correct container for this user (container = corpclaw_agent_{user.id}).
        """
        # Extract user injected by registry — not passed to the container
        user: User | None = kwargs.pop("user", None)
        user_id: int = int(user.telegram_id) if user and user.telegram_id else 0

        if user_id == 0:
            raise ContainerIPCError(0, "Cannot dispatch to container — user ID unknown")

        logger.debug("IPCToolProxy: %s → container for user %d", self.name, user_id)
        return await self._ipc.send_tool_call(
            user_id=user_id,
            tool_name=self.name,
            args=kwargs,
        )
