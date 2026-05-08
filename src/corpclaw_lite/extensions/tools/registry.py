from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from corpclaw_lite.extensions.tools.base import Tool

__all__ = [
    "ToolRegistry",
]

if TYPE_CHECKING:
    from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry for managing and executing agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._description_overrides: dict[str, dict[str, Any]] = {}

    def register(self, tool: Tool, *, allow_replace: bool = False) -> None:
        """Register a tool.

        Args:
            tool: The tool to register.
            allow_replace: If True, silently replace an existing tool with the
                same name (used by MCPHotReloader). If False (default), raises
                ValueError on name conflict.
        """
        if tool.name in self._tools and not allow_replace:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool by name. No-op if the tool is not registered."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_all(self) -> list[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def items(self) -> dict[str, Tool]:
        """Return a copy of the name→tool mapping."""
        return dict(self._tools)

    def load_overrides(self, path: Path | str) -> None:
        """Load tool description overrides from a YAML file.

        Expected format::

            overrides:
              tool_name:
                description: "new description"
                params:
                  param_name:
                    description: "new param description"
        """
        path = Path(path)
        if not path.exists():
            return
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        overrides: dict[str, Any] = data.get("overrides", {})
        self._description_overrides.update(overrides)

    def load_overrides_dict(self, overrides: dict[str, Any]) -> None:
        """Load tool description overrides from a dictionary (used by calibration)."""
        self._description_overrides.update(overrides)

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

        Results are scrubbed for credentials before being returned so that
        API keys or tokens inside read files never reach the LLM context or
        user-facing responses.
        """
        tool = self.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found."

        try:
            result = await tool.execute(**arguments, user=user)
        except Exception as e:
            logger.exception("Tool '%s' execution failed", name)
            return f"Error executing '{name}': {type(e).__name__}: {e}"

        from corpclaw_lite.security.credential_scrubber import scrub_text

        return scrub_text(result)

    def _build_schema(self, tool: Tool) -> dict[str, Any]:
        """Build a single OpenAI function-calling schema for a tool.

        Applies calibration description overrides if present.
        """
        override = self._description_overrides.get(tool.name)
        tool_description = (
            override["description"] if override and "description" in override else tool.description
        )
        param_overrides: dict[str, Any] = override.get("params", {}) if override else {}

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in tool.params:
            p_override = param_overrides.get(param.name, {})
            param_desc = p_override.get("description", param.description)

            param_def: dict[str, Any] = {
                "type": param.type,
                "description": param_desc,
            }
            if param.enum:
                param_def["enum"] = param.enum

            properties[param.name] = param_def
            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool_description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def to_schemas(self) -> list[dict[str, Any]]:
        """Convert all registered tools to OpenAI function calling schemas.

        If calibration overrides are loaded, they take priority for descriptions.
        """
        return [self._build_schema(tool) for tool in self._tools.values()]

    def to_schemas_for_user(
        self,
        permission_checker: Any,
        user: Any,
    ) -> list[dict[str, Any]]:
        """Like to_schemas(), but excludes tools the user cannot use.

        Falls back to the full schema list if either argument is None.
        This prevents the LLM from wasting tokens on tools it cannot invoke.
        """
        if permission_checker is None or user is None:
            return self.to_schemas()

        return [
            self._build_schema(tool)
            for tool in self._tools.values()
            if permission_checker.can_use_tool(user, tool.name)
        ]
