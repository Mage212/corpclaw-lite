"""normalize_excel — Excel file normalization tool using openpyxl."""

from __future__ import annotations

import re
from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.builtin.files import resolve_and_validate_path

__all__ = [
    "NormalizeExcelTool",
]


class NormalizeExcelTool(Tool):
    """Normalize an Excel (.xlsx) file: fix headers, remove duplicates and empty rows."""

    name = "normalize_excel"
    description = "Normalize an Excel (.xlsx) file: fix headers, remove duplicates and empty rows."
    params = [
        ToolParam(name="path", type="string", description="Path to .xlsx file"),
        ToolParam(
            name="output_path",
            type="string",
            description="Output path (default: {name}_normalized.xlsx)",
            required=False,
        ),
        ToolParam(
            name="remove_duplicates",
            type="boolean",
            description="Remove duplicate rows (default true)",
            required=False,
        ),
        ToolParam(
            name="normalize_headers",
            type="boolean",
            description="Normalize headers to snake_case (default true)",
            required=False,
        ),
        ToolParam(
            name="remove_empty_rows",
            type="boolean",
            description="Remove completely empty rows (default true)",
            required=False,
        ),
    ]
    risk_level = RiskLevel.MEDIUM

    async def execute(self, **kwargs: Any) -> str:  # noqa: C901
        path_str = kwargs.get("path")
        if not path_str or not isinstance(path_str, str):
            return "Error: 'path' parameter is required."

        try:
            resolved = resolve_and_validate_path(path_str)
        except PermissionError as e:
            return f"Error: {e}"

        if not resolved.exists():
            return f"Error: File '{path_str}' does not exist."

        if resolved.suffix.lower() != ".xlsx":
            return "Error: Only .xlsx files are supported. Use a different tool for CSV."

        # Output path
        output_str = kwargs.get("output_path")
        if output_str and isinstance(output_str, str):
            try:
                output_path = resolve_and_validate_path(output_str)
            except PermissionError as e:
                return f"Error: {e}"
        else:
            output_path = resolved.parent / f"{resolved.stem}_normalized.xlsx"

        do_headers = kwargs.get("normalize_headers", True)
        do_dedup = kwargs.get("remove_duplicates", True)
        do_empty = kwargs.get("remove_empty_rows", True)

        try:
            import openpyxl  # type: ignore[import-untyped]
        except ImportError:
            return "Error: openpyxl is not installed."

        try:
            wb = openpyxl.load_workbook(str(resolved))
        except Exception as e:
            return f"Error reading Excel file: {e}"

        ws = wb.active
        if ws is None:
            return "Error: Workbook has no active sheet."

        stats: dict[str, int] = {"headers_changed": 0, "duplicates_removed": 0, "empty_removed": 0}
        total_rows = ws.max_row or 0

        # 1. Normalize headers (first row)
        if do_headers and total_rows > 0:
            for col in range(1, (ws.max_column or 0) + 1):
                cell = ws.cell(row=1, column=col)
                if cell.value and isinstance(cell.value, str):
                    original = cell.value
                    normalized = _normalize_header(original)
                    if normalized != original:
                        cell.value = normalized  # type: ignore[misc]
                        stats["headers_changed"] += 1

        # 2. Collect data rows (skip header)
        rows_to_delete: list[int] = []

        if total_rows > 1:
            seen: set[tuple[Any, ...]] = set()
            for row_idx in range(total_rows, 1, -1):  # reverse to safely delete
                values = tuple(
                    ws.cell(row=row_idx, column=c).value for c in range(1, (ws.max_column or 0) + 1)
                )

                # Empty row check
                is_empty = all(
                    v is None or (isinstance(v, str) and v.strip() == "") for v in values
                )
                if do_empty and is_empty:
                    rows_to_delete.append(row_idx)
                    stats["empty_removed"] += 1
                    continue

                # Duplicate check
                if do_dedup:
                    key = tuple(str(v) if v is not None else "" for v in values)
                    if key in seen:
                        rows_to_delete.append(row_idx)
                        stats["duplicates_removed"] += 1
                    else:
                        seen.add(key)

        # Delete rows (already in reverse order)
        for row_idx in rows_to_delete:
            ws.delete_rows(row_idx)

        try:
            wb.save(str(output_path))
        except Exception as e:
            return f"Error saving file: {e}"

        data_rows = max(0, total_rows - 1)
        remaining = data_rows - stats["duplicates_removed"] - stats["empty_removed"]

        parts = [f"Normalized '{resolved.name}' → '{output_path.name}'"]
        parts.append(f"Rows processed: {data_rows}, remaining: {remaining}")
        if stats["headers_changed"]:
            parts.append(f"Headers normalized: {stats['headers_changed']}")
        if stats["duplicates_removed"]:
            parts.append(f"Duplicates removed: {stats['duplicates_removed']}")
        if stats["empty_removed"]:
            parts.append(f"Empty rows removed: {stats['empty_removed']}")
        parts.append(f"Output: {output_path}")

        return "\n".join(parts)


def _normalize_header(header: str) -> str:
    """Normalize a column header to snake_case."""
    h = header.strip()
    h = re.sub(r"[^\w\s]", "", h)
    h = re.sub(r"\s+", "_", h)
    return h.lower()
