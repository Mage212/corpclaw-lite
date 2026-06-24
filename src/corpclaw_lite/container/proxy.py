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

    # Host-side kwargs that ToolRegistry injects for every call but which must
    # NEVER cross the IPC boundary — they are live Python objects (User, run_id,
    # progress callbacks, trajectory recorders) that are (a) not JSON-serializable
    # and (b) meaningless inside the container. Only LLM-supplied arguments reach
    # the container; container-side tools read their own params via ``**kwargs``.
    _HOST_ONLY_KWARGS: frozenset[str] = frozenset(
        {
            "user",
            "run_id",
            "on_subagent_tool_start",
            "on_subagent_tool_batch_start",
            "on_subagent_llm_stage",
            "on_subagent_llm_queue_status",
            "parent_trajectory_recorder",
        }
    )

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
        # Names of the parameters the wrapped tool declares in its schema — the
        # only arguments we forward to the container.
        self._declared_param_names: set[str] = {p.name for p in params}

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

        Only LLM-supplied arguments are forwarded to the container. Host-side
        context injected by ToolRegistry (``user``, ``run_id``, progress
        callbacks, trajectory recorders) is stripped here — those are live Python
        objects that cannot be serialized over IPC and are not meaningful inside
        the container. The 'user' kwarg resolves the target container
        (``corpclaw_agent_{user.id}``).

        Args are filtered to the wrapped tool's declared parameters so that
        nothing unserializable ever reaches ``IPCAuth.sign``.
        """
        user: User | None = kwargs.pop("user", None)
        user_id: int = int(user.id) if user else 0

        if user_id == 0:
            raise ContainerIPCError(0, "Cannot dispatch to container — user ID unknown")

        # Drop all host-injected context, then keep only declared params.
        # This is defence-in-depth: even if a new host kwarg is added to
        # ToolRegistry in the future, it cannot leak into the IPC payload.
        for key in self._HOST_ONLY_KWARGS:
            kwargs.pop(key, None)
        args = {name: value for name, value in kwargs.items() if name in self._declared_param_names}

        logger.debug("IPCToolProxy: %s → container for user %d", self.name, user_id)
        return await self._ipc.send_tool_call(
            user_id=user_id,
            tool_name=self.name,
            args=args,
        )
