"""excel_workbook -- structured Excel read/fill operations for subagents.

Reads cells by coordinate and fills cells while preserving all formatting,
formulas, and merged ranges.  Designed for working with template-based
corporate reports where structure must be maintained.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.builtin.files import resolve_and_validate_path
from corpclaw_lite.utils.async_helpers import run_in_thread
from corpclaw_lite.utils.fs import atomic_save_via, file_signature

__all__ = ["ExcelWorkbookTool", "fill_by_date", "fill_by_key"]

_MAX_DEFAULT_ROWS = 25
_MAX_ROWS_PER_CALL = 50
_MAX_OUTPUT_CHARS = 10_000
# Workbook headers can appear well above their data region.
# Column lookup by header name scans only this many rows.
_MAX_HEADER_SCAN_ROWS = 15
# Formula-reference scan cap (CR-26): _count_formulas_referencing iterates the
# sheet to find formulas pointing at filled cells. 1000 rows covers any
# realistic template; beyond that, references to just-filled cells are rare.
_MAX_FORMULA_SCAN_ROWS = 1000
_FORMULA_MODES = {"both", "values", "formulas"}
_MISSING_CACHED_VALUE = "<unavailable>"

# Matches an A1-style cell/range reference (absolute or relative) inside a formula,
# e.g. A1, $B$2, C3:D5, Sheet2!E7. Capture group 1 is the cell/range token.
_CELL_REF_RE = re.compile(r"(\$?[A-Z]{1,3}\$?\d{1,7}(?::\$?[A-Z]{1,3}\$?\d{1,7})?)")


def _resolve_sheet(wb: Any, sheet_name: str | None) -> Any:
    """Get worksheet by name or fall back to active sheet.

    Does NOT close ``wb`` on miss — cleanup is the caller's responsibility.
    Every caller wraps this in ``try/finally wb.close()``; closing here was a
    surprising side effect on a read-named helper (CR-9).
    """
    if sheet_name is not None:
        if sheet_name not in wb.sheetnames:
            raise ValueError(f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}")
        return wb[sheet_name]
    return wb.active


def _normalize_formula_mode(show_formulas: bool, formula_mode: Any) -> str:
    if formula_mode is None or formula_mode == "":
        return "formulas" if show_formulas else "values"
    mode = str(formula_mode).strip().lower()
    if mode not in _FORMULA_MODES:
        allowed = ", ".join(sorted(_FORMULA_MODES))
        raise ValueError(f"Invalid formula_mode '{formula_mode}'. Use one of: {allowed}.")
    return mode


def _is_formula(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("=")


# Characters that spreadsheet applications interpret as the start of a formula.
# Writing such a value verbatim into a cell turns it into a live formula (CWE-1236
# CSV/Formula Injection) when the output .xlsx is opened by a human. Prefixing a
# leading single quote forces the cell to text in Excel/LibreOffice/Numbers.
_FORMULA_INJECTION_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")


def sanitize_cell_value(value: Any) -> Any:
    """Neutralise CSV/Excel formula injection (CWE-1236) on cell writes.

    Strings that begin with a formula-trigger character (=, +, @) are
    prefixed with a single quote so the spreadsheet stores them as literal
    text instead of evaluating them as a formula (DDE commands, HYPERLINK,
    etc.). Numbers and non-string values pass through unchanged. A leading
    minus is only treated as a trigger when the remainder is not a number —
    a plain negative-number string (e.g. ``"-5"``) is written as-is.
    """
    if not isinstance(value, str) or not value:
        return value
    first = value[0]
    if first in ("=", "+", "@"):
        return f"'{value}"
    if first == "-":
        # Quote only if it's not a plain negative number (e.g. "-5", "-5.5").
        try:
            float(value)
        except ValueError:
            return f"'{value}"
    return value


def _expand_cell_refs(refs: set[str]) -> set[str]:
    """Expand any A1:B2 ranges into their individual cell coordinates."""
    from openpyxl.utils import range_boundaries
    from openpyxl.utils.cell import get_column_letter

    expanded: set[str] = set()
    for ref in refs:
        if ":" not in ref:
            expanded.add(ref.replace("$", ""))
            continue
        min_col, min_row, max_col, max_row = range_boundaries(ref)
        if min_row is None or max_row is None or min_col is None or max_col is None:
            continue
        for row in range(min_row, max_row + 1):
            for col in range(min_col, max_col + 1):
                expanded.add(f"{get_column_letter(col)}{row}")
    return expanded


def _count_formulas_referencing(ws: Any, filled_addresses: set[str]) -> int:
    """Count formulas in the sheet whose referenced cells overlap the filled set.

    Uses the static formula text only (openpyxl does not recalculate). Ranges
    like A1:B2 are expanded to individual cells before the intersection check.
    """
    if not filled_addresses:
        return 0
    affected = 0
    # CR-26: cap iteration to avoid O(rows×cols) on huge sheets. 1000 rows
    # covers realistic workbook templates; references to just-filled cells
    # beyond that range are vanishingly rare.
    max_row = min(ws.max_row or 0, _MAX_FORMULA_SCAN_ROWS)
    for row in ws.iter_rows(max_row=max_row):
        for cell in row:
            value = cell.value
            if not _is_formula(value):
                continue
            refs = {m.group(1) for m in _CELL_REF_RE.finditer(str(value))}
            # Strip sheet qualifiers and absolute markers before expansion.
            refs = {r.split("!")[-1] for r in refs}
            refs = {r.replace("$", "") for r in refs}
            if _expand_cell_refs(refs) & filled_addresses:
                affected += 1
    return affected


def _format_value(value: Any, max_chars: int, *, quote_strings: bool = False) -> str:
    if value is None:
        return "None"
    if isinstance(value, datetime):
        if value.time().replace(microsecond=0) == datetime.min.time():
            text = value.strftime("%d.%m.%Y")
        else:
            text = value.strftime("%d.%m.%Y %H:%M:%S")
    elif isinstance(value, date):
        text = value.strftime("%d.%m.%Y")
    elif isinstance(value, str) and quote_strings:
        text = repr(value)
    else:
        text = str(value)
    return text[:max_chars]


def _format_compact_cell(
    formula_cell: Any,
    value_cell: Any | None,
    formula_mode: str,
) -> str | None:
    formula_value = formula_cell.value
    value = value_cell.value if value_cell is not None else formula_value

    if formula_mode == "formulas":
        if formula_value is None:
            return None
        formula_hint = " (formula)" if _is_formula(formula_value) else ""
        return f"{formula_cell.coordinate}={_format_value(formula_value, 50)}{formula_hint}"

    if formula_mode == "values":
        if value is None:
            return None
        return f"{formula_cell.coordinate}={_format_value(value, 50)}"

    if _is_formula(formula_value):
        cached_value = _MISSING_CACHED_VALUE if value is None else _format_value(value, 50)
        return (
            f"{formula_cell.coordinate}=formula:{_format_value(formula_value, 50)} "
            f"| cached_value={cached_value}"
        )
    if value is None:
        return None
    return f"{formula_cell.coordinate}={_format_value(value, 50)}"


def _format_detail_cell(formula_cell: Any, value_cell: Any | None, formula_mode: str) -> str:
    formula_value = formula_cell.value
    value = value_cell.value if value_cell is not None else formula_value

    if formula_mode == "formulas":
        formula_hint = " (formula)" if _is_formula(formula_value) else ""
        return (
            f"  {formula_cell.coordinate} = "
            f"{_format_value(formula_value, 60, quote_strings=True)}{formula_hint}"
        )

    if formula_mode == "values":
        return f"  {formula_cell.coordinate} = {_format_value(value, 60, quote_strings=True)}"

    if _is_formula(formula_value):
        cached_value = (
            _MISSING_CACHED_VALUE if value is None else _format_value(value, 60, quote_strings=True)
        )
        return (
            f"  {formula_cell.coordinate} = formula:{_format_value(formula_value, 60)} "
            f"| cached_value={cached_value}"
        )
    return f"  {formula_cell.coordinate} = {_format_value(value, 60, quote_strings=True)}"


def _read_cells(
    path: Path,
    sheet_name: str | None,
    cells: str,
    formula_mode: str,
    offset: int,
    limit: int,
) -> str:
    import openpyxl

    # Load only what the mode needs. "values" reads a single data_only workbook;
    # "formulas" reads a single formula workbook; "both" needs both.
    # CR-8: when two loads are needed ("both"), the second load is moved inside
    # the try/finally of the first so a failure on the second load (corrupt
    # data_only cache, OOM) cannot leak the first workbook.
    # Memory: openpyxl-workbook-resource-cleanup.
    if formula_mode == "values":
        wb_primary = openpyxl.load_workbook(str(path), data_only=True)
        wb_secondary = None
    elif formula_mode == "formulas":
        wb_primary = openpyxl.load_workbook(str(path), data_only=False)
        wb_secondary = None
    else:  # both
        wb_primary = openpyxl.load_workbook(str(path), data_only=False)
        try:
            wb_secondary = openpyxl.load_workbook(str(path), data_only=True)
        except BaseException:
            wb_primary.close()
            raise
    try:
        ws = _resolve_sheet(wb_primary, sheet_name)
        ws_values = _resolve_sheet(wb_secondary, sheet_name) if wb_secondary is not None else None
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
                    value_cell = (
                        ws_values.cell(row=row_idx, column=col_idx)
                        if ws_values is not None
                        else None
                    )
                    formatted = _format_compact_cell(cell, value_cell, formula_mode)
                    if formatted is not None:
                        row_data.append(formatted)
                if row_data:
                    lines.append(f"  Row {row_idx}: {'  |  '.join(row_data)}")
                    data_rows += 1

            if data_rows == limit and end_row <= max_row:
                lines.append(
                    f"  Showing rows {start_row}-{end_row - 1}. "
                    f"More rows may exist — call again with offset={offset + limit}."
                )
            return "\n".join(lines)

        def append_range(range_ref: str) -> None:
            """Append a compact row-based view for one rectangular range."""
            lines.append(f"Range: {range_ref}")
            try:
                rows_in_range = list(ws[range_ref])
            except Exception as e:
                lines.append(f"  Error: invalid range '{range_ref}': {e}")
                return

            total_rows = len(rows_in_range)
            start_idx = min(offset, total_rows)
            end_idx = min(start_idx + limit, total_rows)
            data_rows = 0

            for row in rows_in_range[start_idx:end_idx]:
                row_data: list[str] = []
                for cell in row:
                    value_cell = ws_values[cell.coordinate] if ws_values is not None else None
                    formatted = _format_compact_cell(cell, value_cell, formula_mode)
                    if formatted is not None:
                        row_data.append(formatted)
                if row_data:
                    lines.append(f"  Row {row[0].row}: {'  |  '.join(row_data)}")
                    data_rows += 1

            if data_rows == limit and end_idx < total_rows:
                lines.append(
                    f"  Showing rows {start_idx + 1}-{end_idx} of range. "
                    f"More rows may exist — call again with offset={offset + limit}."
                )

        def append_cell(cell_ref: str) -> None:
            """Append a detailed view for one cell reference."""
            try:
                cell = ws[cell_ref]
            except Exception:
                lines.append(f"  {cell_ref} = Error: invalid cell reference")
                return
            if not hasattr(cell, "coordinate"):
                lines.append(f"  {cell_ref} = Error: invalid cell reference")
                return
            value_cell = ws_values[cell.coordinate] if ws_values is not None else None
            lines.append(_format_detail_cell(cell, value_cell, formula_mode))

        # Parse cells specification. Supports one range ("B2:F7"), one cell ("A1"), or
        # a comma-separated mix ("A1,B2:D4,F8:G9").
        for cell_spec in [part.strip() for part in cells.split(",") if part.strip()]:
            if ":" in cell_spec:
                append_range(cell_spec)
            else:
                append_cell(cell_spec)

        return "\n".join(lines)
    finally:
        wb_primary.close()
        if wb_secondary is not None:
            wb_secondary.close()


_DATE_KEY_FORMATS = (
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
)


def _parse_date_key(value: Any) -> date | None:
    """Normalize a cell value or JSON key into a ``date``, or None if not a date.

    Rejects reporting-period strings such as ``2030-03-01 - 2030-03-10`` so they
    are not mistaken for a single day when scanning the date column.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # Excel serial dates are uncommon as JSON keys; skip bare numbers.
        return None
    text = str(value).strip()
    if not text:
        return None
    # Period / range labels (campaign or reporting period) — not a single date.
    if " - " in text or " – " in text or " — " in text:
        return None
    if text.count(".") >= 4 or text.count("-") >= 4 or text.count("/") >= 4:
        return None
    # ISO datetime: 2026-07-06T00:00:00 or 2026-07-06 00:00:00
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    if " " in text:
        # Allow "YYYY-MM-DD HH:MM:SS" only; reject other spaced strings.
        head, _, tail = text.partition(" ")
        if not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", tail.strip()):
            return None
        text = head
    for fmt in _DATE_KEY_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# Formula date columns can lose data_only caches after an openpyxl re-save.
_LEFT_DATE_RE = re.compile(
    r"^=LEFT\(\$?([A-Z]{1,3})\$?(\d{1,7})\s*,\s*10\s*\)$",
    re.IGNORECASE,
)
_DATE_OFFSET_RE = re.compile(
    r"^=\$?([A-Z]{1,3})\$?(\d{1,7})\s*([+-])\s*(\d+)\s*$",
    re.IGNORECASE,
)


def _parse_period_start(value: Any) -> date | None:
    """Parse the start date from a period label like ``01.07.2026 - 31.07.2026``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    direct = _parse_date_key(text)
    if direct is not None:
        return direct
    for sep in (" - ", " – ", " — "):
        if sep in text:
            return _parse_date_key(text.split(sep, 1)[0].strip())
    # LEFT(...,10) style: first 10 chars of a longer period string.
    if len(text) >= 10:
        return _parse_date_key(text[:10])
    return None


def _eval_simple_date_formula(
    ws: Any,
    row: int,
    col: int,
    memo: dict[tuple[int, int], date | None] | None = None,
) -> date | None:
    """Evaluate common date formulas when cached values are missing.

    Supports ``=LEFT($F$5,10)`` (campaign period start) and ``=F16±N`` chains
    (both ascending ``=F16+1`` and descending ``=F16-1`` date grids).
    """
    from datetime import timedelta

    from openpyxl.utils import column_index_from_string

    if memo is None:
        memo = {}
    key = (row, col)
    if key in memo:
        return memo[key]
    # Guard against cycles while resolving.
    memo[key] = None

    raw = ws.cell(row=row, column=col).value
    parsed = _parse_date_key(raw)
    if parsed is not None:
        memo[key] = parsed
        return parsed
    if not isinstance(raw, str) or not raw.startswith("="):
        return None

    formula = raw.strip()
    left_match = _LEFT_DATE_RE.match(formula)
    if left_match is not None:
        src_col = int(column_index_from_string(left_match.group(1).upper()))
        src_row = int(left_match.group(2))
        start = _parse_period_start(ws.cell(row=src_row, column=src_col).value)
        memo[key] = start
        return start

    offset_match = _DATE_OFFSET_RE.match(formula)
    if offset_match is not None:
        base_col = int(column_index_from_string(offset_match.group(1).upper()))
        base_row = int(offset_match.group(2))
        sign = offset_match.group(3)
        days = int(offset_match.group(4))
        if sign == "-":
            days = -days
        base = _eval_simple_date_formula(ws, base_row, base_col, memo)
        if base is None:
            return None
        result = base + timedelta(days=days)
        memo[key] = result
        return result

    return None


def _column_letter_to_index(letter: str) -> int:
    from openpyxl.utils import column_index_from_string

    return int(column_index_from_string(letter.strip().upper()))


def _resolve_column_index(ws: Any, column_spec: str) -> int:
    """Resolve a column letter (``F``) or header name (``Факт`` / ``Дата``) to 1-based index."""
    spec = column_spec.strip()
    if not spec:
        raise ValueError("Column spec must be a non-empty letter or header name.")
    # Pure letter(s): A, F, AA
    if re.fullmatch(r"[A-Za-z]{1,3}", spec):
        return _column_letter_to_index(spec)

    # Header match in the first _MAX_HEADER_SCAN_ROWS rows supports delayed grids.
    needle = spec.casefold()
    max_row = min(ws.max_row or 1, _MAX_HEADER_SCAN_ROWS)
    max_col = ws.max_column or 1
    for row_idx in range(1, max_row + 1):
        for col_idx in range(1, max_col + 1):
            cell_val = ws.cell(row=row_idx, column=col_idx).value
            if cell_val is None:
                continue
            if str(cell_val).strip().casefold() == needle:
                return col_idx
    raise ValueError(
        f"Column '{column_spec}' not found as a letter or header in the first {max_row} rows."
    )


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip() == ""


def _parse_values_map(values_raw: str | dict[str, Any] | None) -> dict[date, Any]:
    if values_raw is None or values_raw == "":
        raise ValueError(
            "'values' is required for fill_by_date. Provide JSON like {\"2030-03-08\": 100}."
        )
    if isinstance(values_raw, dict):
        parsed_obj: object = values_raw
    else:
        try:
            parsed_obj = json.loads(values_raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"Invalid JSON in 'values': {e}") from e
    if not isinstance(parsed_obj, dict):
        raise ValueError("'values' must be a JSON object mapping dates to values.")
    raw_items = cast(dict[Any, Any], parsed_obj)
    if not raw_items:
        raise ValueError("'values' must be a non-empty JSON object.")

    result: dict[date, Any] = {}
    bad_keys: list[str] = []
    for raw_key, raw_val in raw_items.items():
        key = str(raw_key)
        parsed_date = _parse_date_key(key)
        if parsed_date is None:
            bad_keys.append(key)
            continue
        result[parsed_date] = raw_val
    if bad_keys:
        raise ValueError(f"Could not parse date keys: {', '.join(bad_keys)}")
    return result


def fill_by_date(
    path: Path,
    sheet_name: str | None,
    date_column: str,
    value_column: str,
    values_raw: str | dict[str, Any] | None,
    output_path: Path,
    *,
    period_cell: str | None = None,
    period_value: Any = None,
    only_empty: bool = True,
    dry_run: bool = False,
) -> str:
    """Public wrapper for date-keyed workbook fill (used by FillPlan apply)."""
    return _fill_by_date(
        path,
        sheet_name,
        date_column,
        value_column,
        values_raw,
        output_path,
        period_cell=period_cell,
        period_value=period_value,
        only_empty=only_empty,
        dry_run=dry_run,
    )


def fill_by_key(
    path: Path,
    sheet_name: str | None,
    key_column: str,
    value_column: str,
    values_raw: str | dict[str, Any] | None,
    output_path: Path,
    *,
    period_cell: str | None = None,
    period_value: Any = None,
    only_empty: bool = True,
    dry_run: bool = False,
) -> str:
    """Public wrapper for arbitrary label-keyed fill."""
    return _fill_by_key(
        path,
        sheet_name,
        key_column,
        value_column,
        values_raw,
        output_path,
        period_cell=period_cell,
        period_value=period_value,
        only_empty=only_empty,
        dry_run=dry_run,
    )


def _parse_key_values_map(values_raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if values_raw is None or values_raw == "":
        raise ValueError(
            "'values' is required for fill_by_key. Provide JSON like {\"Item A\": 11}."
        )
    if isinstance(values_raw, dict):
        parsed_obj: object = values_raw
    else:
        try:
            parsed_obj = json.loads(values_raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"Invalid JSON in 'values': {e}") from e
    if not isinstance(parsed_obj, dict):
        raise ValueError("'values' must be a JSON object mapping keys to values.")
    raw_items = cast(dict[Any, Any], parsed_obj)
    if not raw_items:
        raise ValueError("'values' must be a non-empty JSON object.")
    result: dict[str, Any] = {}
    for raw_key, raw_val in raw_items.items():
        key = str(raw_key).strip()
        if not key:
            continue
        result[key] = raw_val
    if not result:
        raise ValueError("'values' has no usable string keys.")
    return result


def _apply_fill_common(
    ws: Any,
    wb: Any,
    *,
    plan_items: list[tuple[Any, int, Any]],
    value_col: int,
    unmatched: list[str],
    output_path: Path,
    sheet_label: str,
    only_empty: bool,
    dry_run: bool,
    period_cell: str | None,
    period_value: Any,
    action_label: str,
    key_label: str,
    track_formulas: bool,
) -> str:
    """Shared planning-loop + save + message for ``fill_by_date`` / ``fill_by_key``.

    CR-10: ~150 lines of duplicated skeleton (matched/skipped tracking,
    period_cell handling, atomic save, message construction, formula note,
    stop-directive) factored out. The caller resolves ``plan_items`` via its
    own row lookup (date_to_row map / folded-key map) and passes them in.

    CR-1: the SUCCESS branch and "Do not retry" stop-directive are gated on
    ``wrote_n > 0``; an empty write emits ``NOOP:`` so the close-nudge in
    ``loop.py`` does not prematurely stop the main agent.
    """
    from openpyxl.cell.cell import MergedCell
    from openpyxl.utils.cell import get_column_letter

    matched: list[str] = []
    skipped_nonempty: list[str] = []
    skipped_merged: list[str] = []
    planned: list[str] = []
    filled_addresses: set[str] = set()

    for display_key, row_idx, val in plan_items:
        addr = f"{get_column_letter(value_col)}{row_idx}"
        cell = ws.cell(row=row_idx, column=value_col)
        if isinstance(cell, MergedCell):
            skipped_merged.append(addr)
            continue
        if only_empty and not _is_empty_value(cell.value):
            skipped_nonempty.append(f"{addr}({display_key})")
            continue
        planned.append(f"{display_key}→{addr}={val}")
        if not dry_run:
            cell.value = sanitize_cell_value(val)
            filled_addresses.add(addr)
            matched.append(addr)
        else:
            matched.append(addr)

    period_note = ""
    if period_cell and period_value is not None and str(period_value) != "":
        try:
            pcell = ws[period_cell]
        except Exception as e:
            return f"Error: invalid period_cell '{period_cell}': {e}"
        if isinstance(pcell, MergedCell):
            period_note = f" Skipped period_cell {period_cell} (merged non-top-left)."
        elif dry_run:
            period_note = f" Would set {period_cell}={period_value!r}."
        else:
            pcell.value = sanitize_cell_value(period_value)
            filled_addresses.add(period_cell.replace("$", ""))
            period_note = f" Set {period_cell}={period_value!r}."

    if not dry_run:
        try:
            atomic_save_via(wb.save, Path(str(output_path)))
        except Exception as e:
            return f"Error saving file: {e}"

    wrote_n = len(matched)
    already_n = len(skipped_nonempty)
    unmatched_n = len(unmatched)
    merged_n = len(skipped_merged)

    if dry_run:
        msg = (
            f"Dry-run {action_label} on sheet '{sheet_label}': "
            f"would_write={wrote_n}, already_filled={already_n}, "
            f"unmatched={unmatched_n}, skipped_merged={merged_n}."
        )
    elif unmatched_n > 0:
        msg = (
            f"PARTIAL: wrote {wrote_n} gap value(s) to '{sheet_label}' → {output_path.name}. "
            f"wrote={wrote_n}, already_filled={already_n}, unmatched={unmatched_n}, "
            f"skipped_merged={merged_n}."
        )
    elif wrote_n == 0:
        # CR-1: all matched rows skipped (only_empty / merged) but nothing
        # unmatched. Emit NOOP — must NOT trigger the close-nudge that fires
        # on "SUCCESS:" + "gap value", otherwise the main agent would stop on
        # a workbook with zero new data written.
        msg = (
            f"NOOP: 0 new values written to '{sheet_label}' → {output_path.name}. "
            f"already_filled={already_n}, unmatched={unmatched_n}, "
            f"skipped_merged={merged_n}. Verify the sheet is already complete "
            f"or re-check column mapping; no changes made."
        )
    else:
        msg = (
            f"SUCCESS: wrote {wrote_n} gap value(s) to '{sheet_label}' → {output_path.name}. "
            f"wrote={wrote_n}, already_filled={already_n}, unmatched={unmatched_n}, "
            f"skipped_merged={merged_n}."
        )

    if planned:
        preview = "; ".join(planned[:12])
        if len(planned) > 12:
            preview += f"; …(+{len(planned) - 12} more)"
        msg += f" Map: {preview}."
    if unmatched:
        msg += f" Unmatched {key_label} (not found in sheet): {', '.join(unmatched)}."
    if skipped_nonempty and only_empty:
        # Date-mode adds an explicit anti-retry hint because "already filled"
        # is often misread by local LLMs as "wrong date format — retry".
        if action_label == "fill_by_date":
            hint_suffix = ", expected — do NOT retry with another date format"
        else:
            hint_suffix = ""
        msg += (
            f" already_filled (only_empty=true{hint_suffix}): "
            f"{', '.join(skipped_nonempty[:8])}" + ("…" if len(skipped_nonempty) > 8 else "") + "."
        )
    if skipped_merged:
        msg += f" Skipped merged: {', '.join(skipped_merged)}."
    msg += period_note

    # Formula-stale note: only the date path tracked formula-referencing cells
    # historically. CR-10 centralizes it behind ``track_formulas`` so the key
    # path now also warns when its writes invalidate formula caches.
    if not dry_run and track_formulas:
        affected_formulas = _count_formulas_referencing(ws, filled_addresses)
        if affected_formulas > 0:
            msg += (
                f" Note: {affected_formulas} formula(s) reference filled cells; cached values "
                "may be stale. openpyxl "
                f"strips all formula cached values on save; reopen in Excel/LibreOffice "
                f"to repopulate them."
            )

    if not dry_run and unmatched_n == 0 and wrote_n > 0:
        msg += (
            f" Done. Do not retry {action_label} or action=fill for this sheet. "
            "Call submit_report and stop."
        )
    return msg


def _fill_by_key(
    path: Path,
    sheet_name: str | None,
    key_column: str,
    value_column: str,
    values_raw: str | dict[str, Any] | None,
    output_path: Path,
    *,
    period_cell: str | None = None,
    period_value: Any = None,
    only_empty: bool = True,
    dry_run: bool = False,
) -> str:
    """Match string labels in ``key_column`` to ``values`` and write ``value_column``."""
    import openpyxl

    try:
        values_map = _parse_key_values_map(values_raw)
    except ValueError as e:
        return f"Error: {e}"

    wb = openpyxl.load_workbook(str(path))
    try:
        ws = _resolve_sheet(wb, sheet_name)
        if ws is None:
            return "Error: Workbook has no sheets."

        try:
            key_col = _resolve_column_index(ws, key_column)
            value_col = _resolve_column_index(ws, value_column)
        except ValueError as e:
            return f"Error: {e}"

        # casefold label → (original display key, row). First occurrence wins;
        # skip summary rows and duplicates.
        label_to_row: dict[str, tuple[str, int]] = {}
        max_row = ws.max_row or 0
        for row_idx in range(1, max_row + 1):
            raw = ws.cell(row=row_idx, column=key_col).value
            if raw is None:
                continue
            label = str(raw).strip()
            if not label:
                continue
            folded = label.casefold()
            if folded in _SKIP_KEY_MARKERS or folded in label_to_row:
                continue
            label_to_row[folded] = (label, row_idx)

        # Build plan_items + unmatched list (key-mode specific lookup), then
        # delegate the shared planning-loop / save / message to _apply_fill_common.
        values_folded = {
            str(k).strip().casefold(): (str(k).strip(), v) for k, v in values_map.items()
        }
        plan_items: list[tuple[Any, int, Any]] = []
        for folded, (display_key, val) in values_folded.items():
            hit = label_to_row.get(folded)
            if hit is None:
                continue
            _label, row_idx = hit
            plan_items.append((display_key, row_idx, val))
        unmatched = sorted(
            display for folded, (display, _) in values_folded.items() if folded not in label_to_row
        )

        return _apply_fill_common(
            ws,
            wb,
            plan_items=plan_items,
            value_col=value_col,
            unmatched=unmatched,
            output_path=output_path,
            sheet_label=sheet_name or ws.title,
            only_empty=only_empty,
            dry_run=dry_run,
            period_cell=period_cell,
            period_value=period_value,
            action_label="fill_by_key",
            key_label="keys",
            track_formulas=True,
        )
    finally:
        wb.close()


_SKIP_KEY_MARKERS = frozenset({"всего", "total", "итого", "#"})


def _fill_by_date(
    path: Path,
    sheet_name: str | None,
    date_column: str,
    value_column: str,
    values_raw: str | dict[str, Any] | None,
    output_path: Path,
    *,
    period_cell: str | None = None,
    period_value: Any = None,
    only_empty: bool = True,
    dry_run: bool = False,
) -> str:
    """Match dates in ``date_column`` to ``values`` and write into ``value_column``."""
    import openpyxl

    try:
        values_map = _parse_values_map(values_raw)
    except ValueError as e:
        return f"Error: {e}"

    # Some templates keep daily dates as formulas. Scan cached
    # values via data_only, but write into the formula workbook so formatting
    # and formulas elsewhere are preserved.
    # CR-8: each load_workbook gets its own try/finally so a failure on the
    # second load (corrupt data_only cache, OOM, file lock) cannot leak the
    # first workbook. Memory: openpyxl-workbook-resource-cleanup.
    wb = openpyxl.load_workbook(str(path))
    try:
        wb_values = openpyxl.load_workbook(str(path), data_only=True)
        try:
            ws = _resolve_sheet(wb, sheet_name)
            ws_values = _resolve_sheet(wb_values, sheet_name)
            if ws is None or ws_values is None:
                return "Error: Workbook has no sheets."

            try:
                date_col = _resolve_column_index(ws, date_column)
                value_col = _resolve_column_index(ws, value_column)
            except ValueError as e:
                return f"Error: {e}"

            # Scan date column → row index for each date (first match wins).
            # Prefer data_only caches; if missing (openpyxl re-save strip),
            # evaluate supported formulas (=LEFT($D$1,10) / =A2+1) on the
            # formula sheet.
            date_to_row: dict[date, int] = {}
            formula_memo: dict[tuple[int, int], date | None] = {}
            max_row = max(ws.max_row or 0, ws_values.max_row or 0)
            for row_idx in range(1, max_row + 1):
                raw_date = ws_values.cell(row=row_idx, column=date_col).value
                cell_date = _parse_date_key(raw_date)
                if cell_date is None:
                    formula_raw = ws.cell(row=row_idx, column=date_col).value
                    cell_date = _parse_date_key(formula_raw)
                    if (
                        cell_date is None
                        and isinstance(formula_raw, str)
                        and formula_raw.startswith("=")
                    ):
                        cell_date = _eval_simple_date_formula(ws, row_idx, date_col, formula_memo)
                if cell_date is None or cell_date in date_to_row:
                    continue
                date_to_row[cell_date] = row_idx

            # Build plan_items + unmatched list (date-mode specific lookup), then
            # delegate the shared planning-loop / save / message to
            # _apply_fill_common.
            plan_items: list[tuple[Any, int, Any]] = []
            for d, val in values_map.items():
                row_idx = date_to_row.get(d)
                if row_idx is None:
                    continue
                plan_items.append((d, row_idx, val))
            unmatched = sorted(d.isoformat() for d in values_map if d not in date_to_row)

            return _apply_fill_common(
                ws,
                wb,
                plan_items=plan_items,
                value_col=value_col,
                unmatched=unmatched,
                output_path=output_path,
                sheet_label=sheet_name or ws.title,
                only_empty=only_empty,
                dry_run=dry_run,
                period_cell=period_cell,
                period_value=period_value,
                action_label="fill_by_date",
                key_label="dates",
                track_formulas=True,
            )
        finally:
            wb_values.close()
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
                cell.value = sanitize_cell_value(value)
                filled.append(addr)
            except Exception as e:
                return f"Error writing to {addr}: {e}"

        try:
            atomic_save_via(wb.save, Path(str(output_path)))
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
        # openpyxl does not recalculate formulas. If any formula references the
        # cells we just filled, its cached value is now stale until the workbook
        # is reopened in Excel/LibreOffice. Surface this so the agent does not
        # treat the old cached values as current.
        affected_formulas = _count_formulas_referencing(ws, set(filled))
        if affected_formulas > 0:
            msg += (
                f" Note: {affected_formulas} formula(s) reference filled cells; cached values "
                "may be stale. openpyxl "
                f"strips all formula cached values on save; reopen in Excel/LibreOffice "
                f"to repopulate them."
            )
        return msg
    finally:
        wb.close()


def _resolve_fill_output_path(
    resolved: Path,
    *,
    in_place: bool,
    output_str: str | None,
) -> Path | str:
    """Return output Path, or an error string starting with ``Error:``."""
    if in_place:
        return resolved
    if output_str:
        try:
            output_path = resolve_and_validate_path(output_str)
        except PermissionError as e:
            return f"Error: {e}"
        if output_path.suffix.lower() != ".xlsx":
            return "Error: output_path must end with .xlsx."
        return output_path
    return resolved.parent / f"{resolved.stem}_filled{resolved.suffix}"


class ExcelWorkbookTool(Tool):
    """Structured Excel operations: read cells by coordinate, fill cells
    preserving all formatting and formulas."""

    name = "excel_workbook"
    description = (
        "Read Excel cells by coordinate. Default: first 25 non-empty rows. "
        "Use 'cells' for specific ranges. Use 'offset'/'limit' for pagination "
        "(max 50 rows per call). If output shows 'More rows may exist', "
        "call again with increased offset to continue reading. For fill action, "
        "the default is a safe <name>_filled.xlsx copy; use in_place=true only "
        "when overwriting the original is explicitly requested. Prefer "
        "fill_by_date when rows are keyed by dates: pass date_column, "
        "value_column, and values as a date→value JSON map."
    )
    params = [
        ToolParam(name="path", type="string", description="Path to .xlsx file"),
        ToolParam(
            name="action",
            type="string",
            description="Action: read, fill, or fill_by_date",
            enum=["read", "fill", "fill_by_date"],
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
                "Cell reference(s). For 'read': single cell ('A1'), range ('B2:F7'), "
                "or comma-separated mix ('A1,C3,D5:F8'). For 'fill': JSON object "
                '({"B2": "value", "G15": 150})'
            ),
            required=False,
        ),
        ToolParam(
            name="output_path",
            type="string",
            description=(
                "Output .xlsx path for fill / fill_by_date. Default: <name>_filled.xlsx. "
                "Ignored for read action."
            ),
            required=False,
        ),
        ToolParam(
            name="in_place",
            type="boolean",
            description=(
                "For fill / fill_by_date only: true to overwrite the original workbook explicitly"
            ),
            required=False,
        ),
        ToolParam(
            name="date_column",
            type="string",
            description=(
                "For fill_by_date: column letter (F) or header name (Дата) that holds row dates"
            ),
            required=False,
        ),
        ToolParam(
            name="value_column",
            type="string",
            description=(
                "For fill_by_date: column letter (H) or header name (Факт) to write values into"
            ),
            required=False,
        ),
        ToolParam(
            name="values",
            type="string",
            description=(
                "For fill_by_date: JSON object mapping dates to values, e.g. "
                '{"2030-03-08": 100, "09.03.2030": 7}'
            ),
            required=False,
        ),
        ToolParam(
            name="period_cell",
            type="string",
            description="For fill_by_date: optional cell for reporting period (e.g. F6)",
            required=False,
        ),
        ToolParam(
            name="period_value",
            type="string",
            description="For fill_by_date: value to write into period_cell",
            required=False,
        ),
        ToolParam(
            name="only_empty",
            type="boolean",
            description=(
                "For fill_by_date: if true (default), skip value cells that already have data"
            ),
            required=False,
        ),
        ToolParam(
            name="dry_run",
            type="boolean",
            description="For fill_by_date: if true, return the date→cell map without writing",
            required=False,
        ),
        ToolParam(
            name="show_formulas",
            type="boolean",
            description=(
                "Backward-compatible read option. true means formula_mode='formulas'. "
                "Prefer formula_mode for new calls."
            ),
            required=False,
        ),
        ToolParam(
            name="formula_mode",
            type="string",
            description=(
                "Read formula handling: values (default: value-only data_only read), "
                "both (show formula text together with cached workbook value), or "
                "formulas (formula strings only). Cached values come from the workbook; "
                "formulas are not recalculated."
            ),
            enum=["both", "values", "formulas"],
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
            description="Max rows to return (default: 25, max: 50)",
            required=False,
        ),
        ToolParam(
            name="expected_sig",
            type="string",
            description=(
                "For fill / fill_by_date: the [file_sig:...] value returned by a prior read. "
                "If set and the file has changed since that read, a warning is added to "
                "the result. Optional."
            ),
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
            return "Error: 'action' is required (read, fill, or fill_by_date)."

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
        try:
            formula_mode = _normalize_formula_mode(show_formulas, kwargs.get("formula_mode"))
        except ValueError as e:
            return f"Error: {e}"
        offset = kwargs.get("offset", 0)
        limit = min(kwargs.get("limit", _MAX_DEFAULT_ROWS), _MAX_ROWS_PER_CALL)

        if action == "read":
            try:
                result = await run_in_thread(
                    _read_cells, resolved, sheet_name, cells, formula_mode, offset, limit
                )
                if len(result) > _MAX_OUTPUT_CHARS:
                    result = result[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"
                # Attach a change-detection signature so the caller can pass it back
                # as expected_sig on a subsequent fill to detect concurrent changes.
                return f"{result}\n[file_sig:{file_signature(resolved)}]"
            except ValueError as e:
                return f"Error: {e}"
        if action in ("fill", "fill_by_date"):
            output_path = _resolve_fill_output_path(
                resolved,
                in_place=bool(kwargs.get("in_place", False)),
                output_str=kwargs.get("output_path"),
            )
            if isinstance(output_path, str):
                return output_path

            if action == "fill":
                try:
                    fill_result = await run_in_thread(
                        _fill_cells, resolved, sheet_name, cells, output_path
                    )
                except ValueError as e:
                    return f"Error: {e}"
            else:
                date_column = str(kwargs.get("date_column") or "").strip()
                value_column = str(kwargs.get("value_column") or "").strip()
                if not date_column:
                    return "Error: 'date_column' is required for fill_by_date."
                if not value_column:
                    return "Error: 'value_column' is required for fill_by_date."
                only_empty_raw = kwargs.get("only_empty")
                only_empty = True if only_empty_raw is None else bool(only_empty_raw)
                dry_run = bool(kwargs.get("dry_run", False))
                period_cell_raw = kwargs.get("period_cell")
                period_cell = (
                    str(period_cell_raw).strip() if period_cell_raw not in (None, "") else None
                )
                try:
                    fill_result = await run_in_thread(
                        _fill_by_date,
                        resolved,
                        sheet_name,
                        date_column,
                        value_column,
                        kwargs.get("values"),
                        output_path,
                        period_cell=period_cell,
                        period_value=kwargs.get("period_value"),
                        only_empty=only_empty,
                        dry_run=dry_run,
                    )
                except ValueError as e:
                    return f"Error: {e}"

            # Opt-in read-before-write guard. The caller passes the file_sig from a
            # prior read action as expected_sig; a mismatch means the file changed
            # between the read and this fill. The fill still applied (openpyxl has
            # already written it), so we surface a warning rather than blocking.
            expected_sig = kwargs.get("expected_sig")
            if (
                expected_sig
                and not bool(kwargs.get("dry_run", False))
                and str(expected_sig) != file_signature(resolved)
            ):
                fill_result = (
                    "Warning: file may have changed since last read (signature mismatch). "
                    "Fill applied anyway.\n" + fill_result
                )
            return fill_result
        return f"Error: Unknown action '{action}'. Use 'read', 'fill', or 'fill_by_date'."
