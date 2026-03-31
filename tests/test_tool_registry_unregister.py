"""Tests for ToolRegistry.unregister() and allow_replace parameter."""

from __future__ import annotations

import pytest

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.registry import ToolRegistry


class _DummyTool(Tool):
    risk_level = RiskLevel.LOW

    def __init__(self, name: str) -> None:
        self._name = name

    @property  # type: ignore[override]
    def name(self) -> str:
        return self._name

    @property  # type: ignore[override]
    def description(self) -> str:
        return f"Dummy tool {self._name}"

    @property  # type: ignore[override]
    def params(self) -> list[ToolParam]:
        return []

    async def execute(self, **kwargs: object) -> str:
        return f"result from {self._name}"


def test_unregister_removes_tool() -> None:
    registry = ToolRegistry()
    tool = _DummyTool("my_tool")
    registry.register(tool)
    assert registry.get("my_tool") is not None

    registry.unregister("my_tool")
    assert registry.get("my_tool") is None


def test_unregister_nonexistent_is_noop() -> None:
    registry = ToolRegistry()
    # Should not raise
    registry.unregister("does_not_exist")


def test_register_raises_on_duplicate_by_default() -> None:
    registry = ToolRegistry()
    registry.register(_DummyTool("clash"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_DummyTool("clash"))


def test_register_allow_replace_overwrites() -> None:
    registry = ToolRegistry()
    registry.register(_DummyTool("tool_a"))
    # Should NOT raise
    new_tool = _DummyTool("tool_a")
    registry.register(new_tool, allow_replace=True)
    assert registry.get("tool_a") is new_tool


def test_unregister_then_reregister() -> None:
    """Hot-reload pattern: unregister old, register new."""
    registry = ToolRegistry()
    old = _DummyTool("hot")
    registry.register(old)

    registry.unregister("hot")
    new = _DummyTool("hot")
    registry.register(new)  # allow_replace=False — should work since old is gone
    assert registry.get("hot") is new
