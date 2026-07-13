"""B-107: tool-surface phase filter + BM25 soft-hint."""

from __future__ import annotations

from corpclaw_lite.agent.tool_surface import (
    SOFT_HINT_MARKER,
    ToolSurfacePhase,
    apply_phase_filter,
    detect_tool_surface_phase,
    inject_soft_hint,
    rank_tool_names,
    schema_tool_name,
)


def _schema(*names: str) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"Tool {n} for testing search files write excel",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def test_detect_default_reading() -> None:
    phase = detect_tool_surface_phase(
        user_message="покажи список файлов",
        tools_used=[],
        prev_phase=None,
        closing_mode=False,
        mandate_enabled=False,
        profile="office",
    )
    assert phase is ToolSurfacePhase.READING


def test_detect_executing_keywords() -> None:
    phase = detect_tool_surface_phase(
        user_message="заполни шаблон Excel и сохрани",
        tools_used=[],
        prev_phase=ToolSurfacePhase.READING,
        closing_mode=False,
        mandate_enabled=False,
        profile="office",
    )
    assert phase is ToolSurfacePhase.EXECUTING


def test_detect_sticky_executing() -> None:
    phase = detect_tool_surface_phase(
        user_message="ok",
        tools_used=["write_file"],
        prev_phase=ToolSurfacePhase.EXECUTING,
        closing_mode=False,
        mandate_enabled=False,
        profile="office",
    )
    assert phase is ToolSurfacePhase.EXECUTING


def test_detect_skips_mandate() -> None:
    phase = detect_tool_surface_phase(
        user_message="research something",
        tools_used=[],
        prev_phase=None,
        closing_mode=False,
        mandate_enabled=True,
        profile="office",
    )
    assert phase is None


def test_detect_skips_closing() -> None:
    phase = detect_tool_surface_phase(
        user_message="write file",
        tools_used=[],
        prev_phase=ToolSurfacePhase.READING,
        closing_mode=True,
        mandate_enabled=False,
        profile="office",
    )
    assert phase is None


def test_filter_reading_drops_write() -> None:
    base = _schema("read_file", "write_file", "exec_script", "search_files", "custom_mcp")
    filtered = apply_phase_filter(base, ToolSurfacePhase.READING, profile="office", enabled=True)
    assert filtered is not None
    names = {schema_tool_name(e) for e in filtered}
    assert "read_file" in names
    assert "search_files" in names
    assert "write_file" not in names
    assert "exec_script" not in names
    # unknown MCP kept (fail-open)
    assert "custom_mcp" in names


def test_filter_executing_restores_write_from_base() -> None:
    base = _schema("read_file", "write_file", "excel_workbook")
    reading = apply_phase_filter(base, ToolSurfacePhase.READING, profile="office", enabled=True)
    assert reading is not None
    assert "write_file" not in {schema_tool_name(e) for e in reading}

    executing = apply_phase_filter(base, ToolSurfacePhase.EXECUTING, profile="office", enabled=True)
    assert executing is not None
    names = {schema_tool_name(e) for e in executing}
    assert "write_file" in names
    assert "excel_workbook" in names


def test_filter_main_keeps_dispatch() -> None:
    base = _schema("read_file", "dispatch_subagent", "write_file")
    filtered = apply_phase_filter(base, ToolSurfacePhase.READING, profile="main", enabled=True)
    assert filtered is not None
    names = {schema_tool_name(e) for e in filtered}
    assert "dispatch_subagent" in names


def test_soft_hint_in_messages_not_system() -> None:
    messages: list[dict[str, object]] = [
        {"role": "user", "content": "find the report"},
    ]
    ranked = rank_tool_names("find the report file search", _schema("search_files", "write_file"))
    out = inject_soft_hint(messages, ranked, enabled=True)
    assert any(SOFT_HINT_MARKER in str(m.get("content")) for m in out)
    # second inject replaces previous marker (single hint)
    out2 = inject_soft_hint(out, ["search_files"], enabled=True)
    hints = [m for m in out2 if SOFT_HINT_MARKER in str(m.get("content"))]
    assert len(hints) == 1
    assert messages[0]["content"] == "find the report"  # original list not required mutated


def test_soft_hint_disabled() -> None:
    messages: list[dict[str, object]] = [{"role": "user", "content": "hi"}]
    out = inject_soft_hint(messages, ["search_files"], enabled=False)
    assert out == messages
