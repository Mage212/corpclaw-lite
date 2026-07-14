"""B-078: isolated tests for tool_surface adaptation (no full AgentLoop)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from corpclaw_lite.agent.adaptations.tool_surface import (
    apply_tool_surface,
    inject_tool_soft_hint,
)
from corpclaw_lite.agent.tool_surface import SOFT_HINT_MARKER
from corpclaw_lite.config.settings import ToolSurfaceSettings


def _schema(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"Tool {n}",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _state(
    *,
    tools: list[str],
    mandate_enabled: bool = False,
    closing: bool = False,
) -> MagicMock:
    base = _schema(*tools)
    state = MagicMock()
    state.base_tools_schema = list(base)
    state.tools_schema = list(base)
    state.tool_surface_phase = None
    state.stats.tools_used = []
    state.stats.iterations = 1
    state.stats.run_id = "run-1"
    state.soft_deadline.closing_mode = closing
    state.mandate.enabled = mandate_enabled
    state.mandate.apply_schema_restrict = lambda schema: schema
    state.context.messages = [{"role": "user", "content": "hello"}]
    return state


def test_apply_tool_surface_office_reading_drops_mutating() -> None:
    state = _state(
        tools=["read_file", "table_query", "write_file", "exec_script"],
    )
    settings = ToolSurfaceSettings(enabled=True, soft_hint_enabled=False)
    apply_tool_surface(
        state,
        user_message="покажи файлы",
        settings=settings,
        profile="office",
    )
    names = {str(s.get("function", {}).get("name", "")) for s in (state.tools_schema or [])}
    assert "read_file" in names
    assert "table_query" in names
    assert "write_file" not in names
    assert "exec_script" not in names


def test_apply_tool_surface_disabled_true_noop_preserves_schema() -> None:
    state = _state(tools=["read_file", "write_file"])
    original = list(state.tools_schema)
    # Simulate mandate restrict applied on disabled path
    restricted = [original[0]]

    def _restrict(schema: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        return restricted

    state.mandate.apply_schema_restrict = _restrict
    settings = ToolSurfaceSettings(enabled=False)
    apply_tool_surface(
        state,
        user_message="write everything",
        settings=settings,
        profile="office",
    )
    assert state.tools_schema is restricted


def test_inject_soft_hint_once() -> None:
    state = _state(tools=["search_files", "write_file"])
    settings = ToolSurfaceSettings(enabled=True, soft_hint_enabled=True, soft_hint_top_k=3)
    inject_tool_soft_hint(
        state,
        user_message="find the report file",
        settings=settings,
        profile="main",
    )
    hints = [m for m in state.context.messages if SOFT_HINT_MARKER in str(m.get("content"))]
    assert len(hints) == 1
