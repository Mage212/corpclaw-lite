from __future__ import annotations

from typing import TYPE_CHECKING, Any

from corpclaw_lite.extensions.tools.base import Tool

if TYPE_CHECKING:
    from corpclaw_lite.users.models import User


class ToolRegistry:
    """Registry for managing and executing agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_all(self) -> list[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def items(self) -> dict[str, Tool]:
        """Return a copy of the name→tool mapping."""
        return dict(self._tools)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        user: User | None = None,
    ) -> str:
        """Execute a tool by name with arguments.

        ``user`` is passed as a keyword argument so tools that need user context
        (e.g. DispatchSubagentTool, ReadImageTool) can receive it without the
        LLM having to supply it explicitly.  Tools that do not need it simply
        absorb it via ``**kwargs``.
        """
        tool = self.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found."

        try:
            return await tool.execute(**arguments, user=user)
        except Exception as e:
            return f"Error executing '{name}': {e}"

    def to_schemas(self) -> list[dict[str, Any]]:
        """Convert registered tools to OpenAI function calling schemas."""
        schemas: list[dict[str, Any]] = []
        for tool in self._tools.values():
            properties: dict[str, Any] = {}
            required: list[str] = []

            for param in tool.params:
                param_def: dict[str, Any] = {
                    "type": param.type,
                    "description": param.description,
                }
                if param.enum:
                    param_def["enum"] = param.enum

                properties[param.name] = param_def
                if param.required:
                    required.append(param.name)

            schema = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
            schemas.append(schema)

        return schemas
