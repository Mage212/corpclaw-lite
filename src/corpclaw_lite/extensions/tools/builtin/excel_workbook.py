"""excel_workbook -- structured Excel read/fill operations for subagents.

Reads cells by coordinate and fills cells while preserving all formatting,
formulas, and merged ranges.  Designed for working with template-based
corporate reports where structure must be maintained.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.builtin.files import resolve_and_validate_path
from corpclaw_lite.utils.async_helpers import run_in_thread

__all__ = ["ExcelWorkbookTool"]

_MAX_DEFAULT_ROWS = 20


def _resolve_sheet(wb: Any, sheet_name: str | None) -> Any:
    """Get worksheet by name or fall back to active sheet."""
    if sheet_name is not None:
        if sheet_name not in wb.sheetnames:
            wb.close()
            raise ValueError(f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}")
        return wb[sheet_name]
    return wb.active


def _read_cells(
    path: Path,
    sheet_name: str | None,
    cells: str,
    show_formulas: bool,
) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=not show_formulas)
    try:
        ws = _resolve_sheet(wb, sheet_name)
        if ws is None:
            return "Error: Workbook has no sheets."

        lines: list[str] = [f'Sheet: "{ws.title}"']

        if not cells:
            # Default: first N rows
            max_rows = min(ws.max_row or 0, _MAX_DEFAULT_ROWS)
            max_cols = ws.max_column or 0
            for row_idx in range(1, max_rows + 1):
                row_data: list[str] = []
                for col_idx in range(1, max_cols + 1):
                    cell = ws.cell(row=row_idx, column=col_idx)
                    if cell.value is not None:
                        val = str(cell.value)[:50]
                        row_data.append(f"{cell.coordinate}={val}")
                if row_data:
                    lines.append(f"  Row {row_idx}: {'  |  '.join(row_data)}")
                else:
                    lines.append(f"  Row {row_idx}: (empty)")
            return "\n".join(lines)

        # Parse cells specification
        if ":" in cells and "," not in cells:
            # Range: "B2:F7"
            lines.append(f"Range: {cells}")
            try:
                for row in ws[cells]:
                    for cell in row:
                        val = cell.value
                        is_formula = show_formulas and isinstance(val, str) and val.startswith("=")
                        formula_hint = " (formula)" if is_formula else ""
                        val_str = repr(val)[:60] if val is not None else "None"
                        lines.append(f"  {cell.coordinate} = {val_str}{formula_hint}")
            except Exception as e:
                return f"Error: Invalid range '{cells}': {e}"
        elif "," in cells:
            # Comma-separated: "A1,C3,B2"
            for addr in cells.split(","):
                addr = addr.strip()
                if not addr:
                    continue
                try:
                    cell = ws[addr]
                except Exception:
                    lines.append(f"  {addr} = Error: invalid cell reference")
                    continue
                val = cell.value
                is_formula = show_formulas and isinstance(val, str) and val.startswith("=")
                formula_hint = " (formula)" if is_formula else ""
                val_str = repr(val)[:60] if val is not None else "None"
                lines.append(f"  {cell.coordinate} = {val_str}{formula_hint}")
        else:
            # Single cell
            try:
                cell = ws[cells]
            except Exception:
                return f"Error: Invalid cell reference '{cells}'"
            val = cell.value
            is_formula = show_formulas and isinstance(val, str) and val.startswith("=")
            formula_hint = " (formula)" if is_formula else ""
            val_str = repr(val)[:60] if val is not None else "None"
            lines.append(f"  {cell.coordinate} = {val_str}{formula_hint}")

        return "\n".join(lines)
    finally:
        wb.close()


def _fill_cells(
    path: Path,
    sheet_name: str | None,
    cells_json: str,
) -> str:
    import openpyxl

    if not cells_json:
        return 'Error: \'cells\' is required for fill action. Provide JSON like {"B2": "value"}.'

    try:
        cells_dict: dict[str, Any] = json.loads(cells_json)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON in 'cells': {e}"

    if not cells_dict:
        return "Error: 'cells' must be a non-empty JSON object {\"address\": value}."

    # Load WITHOUT data_only and read_only to preserve formatting
    wb = openpyxl.load_workbook(str(path))
    try:
        ws = _resolve_sheet(wb, sheet_name)
        if ws is None:
            return "Error: Workbook has no sheets."

        filled: list[str] = []
        for addr, value in cells_dict.items():
            try:
                ws[addr] = value
                filled.append(addr)
            except Exception as e:
                return f"Error writing to {addr}: {e}"

        try:
            wb.save(str(path))
        except Exception as e:
            return f"Error saving file: {e}"

        sheet_label = sheet_name or ws.title
        return f"Filled {len(filled)} cells in sheet '{sheet_label}': {', '.join(filled)}. Saved."
    finally:
        wb.close()


class ExcelWorkbookTool(Tool):
    """Structured Excel operations: read cells by coordinate, fill cells
    preserving all formatting and formulas."""

    name = "excel_workbook"
    description = (
        "Structured Excel operations: read cells by coordinate (range or "
        "individual addresses), or fill cells while preserving all formatting, "
        "formulas, and merged ranges."
    )
    params = [
        ToolParam(name="path", type="string", description="Path to .xlsx file"),
        ToolParam(
            name="action",
            type="string",
            description="Action: read or fill",
            enum=["read", "fill"],
        ),
        ToolParam(
            name="sheet_name",
            type="string",
            description="Sheet name (default: active/first sheet)",
            required=False,
        ),
        ToolParam(
            name="cells",
            type="string",
            description=(
                "Cell reference(s). For 'read': range ('B2:F7') or "
                "comma-separated ('A1,C3'). For 'fill': JSON object "
                '({"B2": "value", "G15": 150})'
            ),
            required=False,
        ),
        ToolParam(
            name="show_formulas",
            type="boolean",
            description="Show formulas instead of values (read action only)",
            required=False,
        ),
    ]
    risk_level = RiskLevel.MEDIUM
    parallel_safe = False

    async def execute(self, **kwargs: Any) -> str:
        path_str = kwargs.get("path", "")
        action = kwargs.get("action", "")

        if not path_str:
            return "Error: 'path' is required."
        if not action:
            return "Error: 'action' is required (read or fill)."

        try:
            resolved = resolve_and_validate_path(path_str)
        except PermissionError as e:
            return f"Error: {e}"

        if not resolved.is_file():
            return f"Error: File not found: {path_str}"
        if resolved.suffix.lower() != ".xlsx":
            return "Error: Only .xlsx files are supported."

        sheet_name: str | None = kwargs.get("sheet_name")
        cells = kwargs.get("cells", "")
        show_formulas = kwargs.get("show_formulas", False)

        if action == "read":
            try:
                return await run_in_thread(_read_cells, resolved, sheet_name, cells, show_formulas)
            except ValueError as e:
                return f"Error: {e}"
        if action == "fill":
            try:
                return await run_in_thread(_fill_cells, resolved, sheet_name, cells)
            except ValueError as e:
                return f"Error: {e}"
        return f"Error: Unknown action '{action}'. Use 'read' or 'fill'."
