from __future__ import annotations

import logging
from collections.abc import Callable
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
        run_id: str | None = None,
        permission_checker: Any | None = None,
        enforce_tool_allowlist: bool = True,
        on_subagent_tool_start: Callable[[str, str], None] | None = None,
        on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None = None,
        on_subagent_llm_stage: Callable[[str, str], None] | None = None,
    ) -> str:
        """Execute a tool by name with arguments.

        ``user`` and ``run_id`` are passed as keyword arguments so tools that need
        runtime context (e.g. DispatchSubagentTool, ReadImageTool) can receive it
        without the LLM having to supply it explicitly. Tools that do not need it
        simply absorb it via ``**kwargs``.

        Results are scrubbed for credentials before being returned so that
        API keys or tokens inside read files never reach the LLM context or
        user-facing responses.
        """
        tool = self.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found."

        if (
            permission_checker is not None
            and user is not None
            and not permission_checker.can_use_registered_tool(
                user,
                tool,
                enforce_tool_allowlist=enforce_tool_allowlist,
            )
        ):
            return (
                f"Error: Permission denied. Your department ({user.department})"
                f" cannot use tool '{name}'."
            )

        try:
            tool_kwargs = dict(arguments)
            tool_kwargs["user"] = user
            tool_kwargs["run_id"] = run_id
            if on_subagent_tool_start is not None:
                tool_kwargs["on_subagent_tool_start"] = on_subagent_tool_start
            if on_subagent_tool_batch_start is not None:
                tool_kwargs["on_subagent_tool_batch_start"] = on_subagent_tool_batch_start
            if on_subagent_llm_stage is not None:
                tool_kwargs["on_subagent_llm_stage"] = on_subagent_llm_stage
            result = await tool.execute(**tool_kwargs)
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
        *,
        enforce_tool_allowlist: bool = True,
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
            if permission_checker.can_use_registered_tool(
                user,
                tool,
                enforce_tool_allowlist=enforce_tool_allowlist,
            )
        ]
