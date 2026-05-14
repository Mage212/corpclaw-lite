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

_MAX_DEFAULT_ROWS = 50
_MAX_ROWS_PER_CALL = 100
_MAX_OUTPUT_CHARS = 15_000


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
    offset: int,
    limit: int,
) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=not show_formulas)
    try:
        ws = _resolve_sheet(wb, sheet_name)
        if ws is None:
            return "Error: Workbook has no sheets."

        lines: list[str] = [f'Sheet: "{ws.title}"']

        if not cells:
            # Default: rows with pagination, non-None only
            max_row = ws.max_row or 0
            max_col = ws.max_column or 0
            start_row = offset + 1
            end_row = min(start_row + limit, max_row + 1)
            data_rows = 0

            for row_idx in range(start_row, end_row):
                row_data: list[str] = []
                for col_idx in range(1, max_col + 1):
                    cell = ws.cell(row=row_idx, column=col_idx)
                    if cell.value is not None:
                        val = str(cell.value)[:50]
                        row_data.append(f"{cell.coordinate}={val}")
                if row_data:
                    lines.append(f"  Row {row_idx}: {'  |  '.join(row_data)}")
                    data_rows += 1

            if data_rows == limit and end_row <= max_row:
                lines.append(
                    f"  Showing rows {start_row}-{end_row - 1}. "
                    f"More rows may exist — call again with offset={offset + limit}."
                )
            return "\n".join(lines)

        # Parse cells specification
        if ":" in cells and "," not in cells:
            # Range: "B2:F7" — compact row-based, skip None
            lines.append(f"Range: {cells}")
            try:
                rows_in_range = list(ws[cells])
            except Exception as e:
                return f"Error: Invalid range '{cells}': {e}"

            total_rows = len(rows_in_range)
            start_idx = min(offset, total_rows)
            end_idx = min(start_idx + limit, total_rows)
            data_rows = 0

            for row in rows_in_range[start_idx:end_idx]:
                row_data: list[str] = []
                for cell in row:
                    if cell.value is not None:
                        val = str(cell.value)[:50]
                        is_formula = (
                            show_formulas
                            and isinstance(cell.value, str)
                            and cell.value.startswith("=")
                        )
                        formula_hint = " (formula)" if is_formula else ""
                        row_data.append(f"{cell.coordinate}={val}{formula_hint}")
                if row_data:
                    lines.append(f"  Row {row[0].row}: {'  |  '.join(row_data)}")
                    data_rows += 1

            if data_rows == limit and end_idx < total_rows:
                lines.append(
                    f"  Showing rows {start_idx + 1}-{end_idx} of range. "
                    f"More rows may exist — call again with offset={offset + limit}."
                )
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
    cells_json: str | dict[str, Any],
    output_path: Path,
) -> str:
    import openpyxl
    from openpyxl.cell.cell import MergedCell

    if not cells_json:
        return 'Error: \'cells\' is required for fill action. Provide JSON like {"B2": "value"}.'

    # XML fallback parser may pre-deserialize JSON, delivering a dict directly.
    if isinstance(cells_json, dict):
        cells_dict: dict[str, Any] = cells_json
    else:
        try:
            cells_dict = json.loads(cells_json)
        except (json.JSONDecodeError, TypeError) as e:
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
        skipped_merged: list[str] = []
        for addr, value in cells_dict.items():
            try:
                cell = ws[addr]
                if isinstance(cell, MergedCell):
                    skipped_merged.append(addr)
                    continue
                cell.value = value
                filled.append(addr)
            except Exception as e:
                return f"Error writing to {addr}: {e}"

        try:
            wb.save(str(output_path))
        except Exception as e:
            return f"Error saving file: {e}"

        sheet_label = sheet_name or ws.title
        msg = (
            f"Filled {len(filled)} cells in sheet '{sheet_label}': {', '.join(filled)}. "
            f"Saved to {output_path.name}."
        )
        if skipped_merged:
            msg += (
                f" Skipped {len(skipped_merged)} merged cells"
                f" (non-top-left): {', '.join(skipped_merged)}."
            )
        return msg
    finally:
        wb.close()


class ExcelWorkbookTool(Tool):
    """Structured Excel operations: read cells by coordinate, fill cells
    preserving all formatting and formulas."""

    name = "excel_workbook"
    description = (
        "Read Excel cells by coordinate. Default: first 50 non-empty rows. "
        "Use 'cells' for specific ranges. Use 'offset'/'limit' for pagination "
        "(max 100 rows per call). If output shows 'More rows may exist', "
        "call again with increased offset to continue reading. For fill action, "
        "the default is a safe <name>_filled.xlsx copy; use in_place=true only "
        "when overwriting the original is explicitly requested."
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
            name="output_path",
            type="string",
            description=(
                "Output .xlsx path for fill action. Default: <name>_filled.xlsx. "
                "Ignored for read action."
            ),
            required=False,
        ),
        ToolParam(
            name="in_place",
            type="boolean",
            description="For fill action only: true to overwrite the original workbook explicitly",
            required=False,
        ),
        ToolParam(
            name="show_formulas",
            type="boolean",
            description="Show formulas instead of values (read action only)",
            required=False,
        ),
        ToolParam(
            name="offset",
            type="integer",
            description="Row offset for pagination (0-based). Use to continue reading.",
            required=False,
        ),
        ToolParam(
            name="limit",
            type="integer",
            description="Max rows to return (default: 50, max: 100)",
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
        offset = kwargs.get("offset", 0)
        limit = min(kwargs.get("limit", _MAX_DEFAULT_ROWS), _MAX_ROWS_PER_CALL)

        if action == "read":
            try:
                result = await run_in_thread(
                    _read_cells, resolved, sheet_name, cells, show_formulas, offset, limit
                )
                if len(result) > _MAX_OUTPUT_CHARS:
                    result = result[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"
                return result
            except ValueError as e:
                return f"Error: {e}"
        if action == "fill":
            output_path = resolved.parent / f"{resolved.stem}_filled{resolved.suffix}"
            in_place = bool(kwargs.get("in_place", False))
            output_str = kwargs.get("output_path")
            if in_place:
                output_path = resolved
            elif output_str:
                try:
                    output_path = resolve_and_validate_path(output_str)
                except PermissionError as e:
                    return f"Error: {e}"
                if output_path.suffix.lower() != ".xlsx":
                    return "Error: output_path must end with .xlsx."
            try:
                return await run_in_thread(_fill_cells, resolved, sheet_name, cells, output_path)
            except ValueError as e:
                return f"Error: {e}"
        return f"Error: Unknown action '{action}'. Use 'read' or 'fill'."
