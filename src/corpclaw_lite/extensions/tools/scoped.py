from __future__ import annotations

from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

__all__ = [
    "ScopedTool",
]


class ScopedTool(Tool):
    """Attach source metadata to a tool without changing its implementation."""

    def __init__(
        self,
        tool: Tool,
        *,
        source_kind: str,
        source_name: str,
        allowed_departments: list[str] | None = None,
    ) -> None:
        self._tool = tool
        self.source_kind = source_kind
        self.source_name = source_name
        self.allowed_departments = (
            list(allowed_departments) if allowed_departments is not None else None
        )

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._tool.name

    @property
    def description(self) -> str:  # type: ignore[override]
        return self._tool.description

    @property
    def params(self) -> list[ToolParam]:  # type: ignore[override]
        return self._tool.params

    @property
    def risk_level(self) -> RiskLevel:  # type: ignore[override]
        return self._tool.risk_level

    @property
    def parallel_safe(self) -> bool:  # type: ignore[override]
        return self._tool.parallel_safe

    @property
    def terminal(self) -> bool:  # type: ignore[override]
        return self._tool.terminal

    async def execute(self, **kwargs: Any) -> str:
        return await self._tool.execute(**kwargs)

    def should_return_direct(self, arguments: dict[str, Any], result: str) -> bool:
        return self._tool.should_return_direct(arguments, result)
