"""
Adapter that wraps an MCPToolDef into the corpclaw_lite Tool interface.

This allows MCP server tools to be registered in ToolRegistry and used
transparently alongside built-in tools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from corpclaw_lite.extensions.mcp.client import MCPClient, MCPToolDef
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

__all__ = [
    "MCPToolAdapter",
]

if TYPE_CHECKING:
    from corpclaw_lite.users.models import User


class MCPToolAdapter(Tool):
    """Wraps a single MCPToolDef as a Tool for use in ToolRegistry."""

    risk_level = RiskLevel.MEDIUM

    def __init__(
        self,
        tool_def: MCPToolDef,
        client: MCPClient,
        server_name: str = "unknown",
    ) -> None:
        self._tool_def = tool_def
        self._client = client
        self.source_kind = "mcp"
        self.source_name = server_name

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._tool_def.name

    @property
    def description(self) -> str:  # type: ignore[override]
        return self._tool_def.description

    @property
    def params(self) -> list[ToolParam]:  # type: ignore[override]
        """Convert MCP input_schema properties to ToolParam list."""
        schema = self._tool_def.input_schema
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        result: list[ToolParam] = []
        for param_name, prop in properties.items():
            result.append(
                ToolParam(
                    name=param_name,
                    type=str(prop.get("type", "string")),
                    description=str(prop.get("description", "")),
                    required=param_name in required,
                )
            )
        return result

    async def execute(self, user: User | None = None, **kwargs: Any) -> str:
        try:
            return await self._client.call_tool(self._tool_def.name, kwargs)
        except Exception as e:
            return f"MCP tool '{self._tool_def.name}' error: {e}"
