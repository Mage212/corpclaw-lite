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

        stats: dict[str, int] = {
            "headers_changed": 0,
            "duplicates_removed": 0,
            "empty_removed": 0,
            "values_fixed": 0,
        }
        total_rows = ws.max_row or 0
        total_cols = ws.max_column or 0

        # Mapping of column index (1-based) to its detected type
        col_types: dict[int, str] = {}

        # 1. Detect column types and normalize headers
        if total_rows > 0:
            for col in range(1, total_cols + 1):
                cell = ws.cell(row=1, column=col)
                val = cell.value
                val_str = str(val) if val is not None else ""

                # Detect type for later value normalization
                col_types[col] = _detect_column_type(val_str)

                # Normalize headers if requested
                if do_headers and val and isinstance(val, str):
                    normalized = _normalize_header(val)
                    if normalized != val:
                        cell.value = normalized  # type: ignore[misc]
                        stats["headers_changed"] += 1

        # 2. Process data rows
        rows_to_delete: list[int] = []
        if total_rows > 1:
            seen: set[tuple[Any, ...]] = set()
            for row_idx in range(total_rows, 1, -1):  # reverse to safely delete
                row_raw_values: list[Any] = []
                for c in range(1, total_cols + 1):
                    cell = ws.cell(row=row_idx, column=c)
                    val = cell.value

                    # Fix value based on detected column type
                    fixed_val = _fix_value(val, col_types.get(c, "text"))
                    if fixed_val != val:
                        cell.value = fixed_val  # type: ignore[misc]
                        stats["values_fixed"] += 1

                    row_raw_values.append(fixed_val)

                values = tuple(row_raw_values)

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

        # 3. Bulk delete rows
        if rows_to_delete:
            current_start = rows_to_delete[0]
            current_count = 1
            for r in rows_to_delete[1:]:
                if r == current_start - current_count:
                    current_count += 1
                else:
                    ws.delete_rows(current_start - current_count + 1, current_count)
                    current_start = r
                    current_count = 1
            ws.delete_rows(current_start - current_count + 1, current_count)

        # 4. Save
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
        if stats["values_fixed"]:
            parts.append(f"Values cleaned/fixed: {stats['values_fixed']}")
        if stats["duplicates_removed"]:
            parts.append(f"Duplicates removed: {stats['duplicates_removed']}")
        if stats["empty_removed"]:
            parts.append(f"Empty rows removed: {stats['empty_removed']}")
        # Return only the filename (not absolute path) so the model can pass it directly to send_file.
        # Absolute paths confuse small local LLMs — they don't know how to translate them.
        parts.append(f"Output filename: {output_path.name}")

        return "\n".join(parts)


def _detect_column_type(header: str) -> str:
    """Detect if column is INN, Date, Numeric or Text based on header name."""
    low = header.lower().strip()
    if "инн" in low:
        return "inn"
    if "дата" in low:
        return "date"
    if any(k in low for k in ["сумма", "инвентарь", "количество", "цена", "цена_за_единицу"]):
        return "numeric"
    return "text"


def _fix_value(val: Any, col_type: str) -> Any:
    """Clean and normalize a single cell value."""
    if val is None:
        return None

    # 1. Clean invisible characters if it's a string
    if isinstance(val, str):
        val = _clean_chars(val)
        if not val.strip():
            return None

    # 2. Type-specific fixes
    if col_type == "inn":
        # Handle scientific notation by converting to int then string
        try:
            if isinstance(val, (int, float)):
                # 7.7E+09 -> 7700000000 -> "7700000000"
                s = str(int(val))
                # Restore leading zeros if looks like a short INN
                if len(s) == 9 or len(s) == 11:
                    s = "0" + s
                return s
            if isinstance(val, str):
                s = val.strip()
                if s.replace(".", "").replace("e+", "").isdigit() and "e+" in s.lower():
                    # Scientific notation string -> str
                    return str(int(float(s)))
                return s
        except Exception:
            return val

    elif col_type == "date":
        from datetime import datetime

        if isinstance(val, datetime):
            return val.strftime("%d.%m.%Y")
        # If it's a serial date (float in Excel)
        if isinstance(val, (int, float)) and 40000 < val < 60000:
            from datetime import datetime, timedelta

            return (datetime(1899, 12, 30) + timedelta(days=int(val))).strftime("%d.%m.%Y")
        return val

    elif col_type == "numeric":
        if isinstance(val, float):
            return round(val, 2)
        if isinstance(val, str):
            try:
                s = val.replace(",", ".").strip()
                return round(float(s), 2)
            except ValueError:
                return val

    return val


def _clean_chars(text: str) -> str:
    """Remove invisible and problematic characters."""
    # Common invisible chars
    chars = {
        "\u00ad": "",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\ufeff": "",
        "\u200e": "",
        "\u200f": "",
        "\u2060": "",
        "\u2028": " ",
        "\u2029": " ",
        "\u00a0": " ",
    }
    for char, repl in chars.items():
        text = text.replace(char, repl)
    # Filter out non-printable ASCII except tab/newline
    text = "".join(c for c in text if ord(c) >= 32 or c in "\n\t")
    return text.strip()


def _normalize_header(header: str) -> str:
    """Normalize a column header to snake_case."""
    h = _clean_chars(header)
    h = re.sub(r"[^\w\s]", "", h)
    h = re.sub(r"\s+", "_", h)
    return h.lower()
