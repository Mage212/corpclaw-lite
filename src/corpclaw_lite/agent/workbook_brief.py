"""Deterministic, value-free structural summary of attached workbooks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "FileBrief",
    "SheetBrief",
    "WorkbookBriefBundle",
    "build_workbook_brief",
    "extract_date_values",
    "extract_key_values",
    "format_files_brief_for_agent",
]

FILES_BRIEF_MARKER = "FILES_BRIEF (deterministic, no LLM):"

_DATE_HEADERS = ("дата", "day", "день", "date", "период")
_KEY_HEADERS = (
    "креатив",
    "creative",
    "кампания",
    "campaign",
    "сегмент",
    "segment",
    "название",
    "name",
)
_VALUE_HEADERS = ("значение", "value", "amount", "metric", "показы", "impressions", "imps", "факт")
_FACT_HEADERS = ("факт", "fact")
_PLAN_HEADERS = ("план", "plan")
_SKIP_ROW_MARKERS = ("всего", "total", "итого")


def _empty_sheets() -> list[SheetBrief]:
    return []


def _empty_files() -> list[FileBrief]:
    return []


@dataclass(slots=True)
class SheetBrief:
    name: str
    max_row: int
    max_col: int
    header_row: int | None = None
    date_column: str | None = None
    value_column: str | None = None
    fact_column: str | None = None
    plan_column: str | None = None
    period_reporting_cell: str | None = None
    period_campaign_cell: str | None = None
    days_count_cell: str | None = None
    dates_are_formulas: bool = False
    role: str = "unknown"  # source | template | other
    match_mode: str = "date"  # date | key
    data_rows: int = 0
    date_min: str | None = None
    date_max: str | None = None


@dataclass(slots=True)
class FileBrief:
    path: str
    name: str
    sheets: list[SheetBrief] = field(default_factory=_empty_sheets)


@dataclass(slots=True)
class WorkbookBriefBundle:
    files: list[FileBrief] = field(default_factory=_empty_files)

    def to_dict(self) -> dict[str, Any]:
        return {"files": [asdict(f) for f in self.files]}


def build_workbook_brief(paths: Sequence[Path | str]) -> WorkbookBriefBundle:
    """Analyse one or more xlsx paths into a compact brief bundle."""
    files: list[FileBrief] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".xlsx", ".xlsm"}:
            continue
        files.append(_brief_file(path))
    return WorkbookBriefBundle(files=files)


def format_files_brief_for_agent(bundle: WorkbookBriefBundle, *, max_sample: int = 3) -> str:
    """Return structural metadata without values or workflow instructions."""
    del max_sample  # retained for call-site compatibility; cell samples are intentionally omitted
    lines: list[str] = [
        FILES_BRIEF_MARKER,
        "Structural metadata only. Choose tools according to the user's request.",
        "No workbook metric values are included.",
        "",
        "### TEMPLATE HINTS",
    ]
    template_lines = 0
    for fb in bundle.files:
        for sh in fb.sheets:
            if sh.role != "template":
                continue
            template_lines += 1
            hint_bits: list[str] = [f"file=`{fb.path}`", f"sheet=`{sh.name}`"]
            if sh.match_mode == "key":
                hint_bits.append("match_mode=key")
                if sh.date_column:
                    hint_bits.append(f"key_column={sh.date_column} (use as date_column in plan)")
            else:
                hint_bits.append("match_mode=date")
                if sh.date_column:
                    hint_bits.append(f"date_column={sh.date_column}")
            if sh.fact_column or sh.value_column:
                hint_bits.append(f"value_column={sh.fact_column or sh.value_column}")
            if sh.plan_column:
                hint_bits.append(f"plan_column={sh.plan_column} (do not overwrite)")
            if sh.period_reporting_cell:
                hint_bits.append(f"period_candidate_cell={sh.period_reporting_cell}")
            if sh.period_campaign_cell:
                hint_bits.append(f"other_period_cell={sh.period_campaign_cell}")
            if sh.days_count_cell:
                hint_bits.append(f"count_cell={sh.days_count_cell}")
            if sh.dates_are_formulas:
                hint_bits.append("dates_are_formulas=true")
            lines.append("- " + "; ".join(hint_bits))
    if template_lines == 0:
        lines.append("- (no template sheets detected)")

    lines.append("")
    lines.append("### SOURCE REGIONS")
    region_lines = 0
    for fb in bundle.files:
        for sh in fb.sheets:
            if sh.role != "source":
                continue
            region_lines += 1
            date_col = sh.date_column or "?"
            value_col = sh.value_column or "?"
            if sh.match_mode == "key":
                lines.append(
                    f"- file=`{fb.path}`; sheet=`{sh.name}`; match_mode=key; "
                    f"key_column={date_col} (date_column in plan); "
                    f"value_column={value_col}; rows={sh.data_rows}"
                )
            else:
                date_range = (
                    f"; date_range={sh.date_min}..{sh.date_max}"
                    if sh.date_min and sh.date_max
                    else ""
                )
                lines.append(
                    f"- file=`{fb.path}`; sheet=`{sh.name}`; match_mode=date; "
                    f"date_column={date_col}; value_column={value_col}; "
                    f"rows={sh.data_rows}{date_range}"
                )
    if region_lines == 0:
        lines.append("- (no source regions detected)")

    lines.append("")
    lines.append("### FILE DETAILS")
    for fb in bundle.files:
        lines.append(f"\n## File: {fb.path}")
        for sh in fb.sheets:
            lines.append(
                f"- Sheet `{sh.name}` role={sh.role} "
                f"({sh.max_row}x{sh.max_col}) header_row={sh.header_row}"
            )
            cols: list[str] = []
            if sh.date_column:
                cols.append(f"date={sh.date_column}")
            if sh.value_column:
                cols.append(f"value={sh.value_column}")
            if sh.fact_column:
                cols.append(f"fact={sh.fact_column}")
            if sh.plan_column:
                cols.append(f"plan={sh.plan_column}")
            if cols:
                lines.append(f"  columns: {', '.join(cols)}")
            if sh.period_reporting_cell:
                lines.append(f"  period_candidate_cell: {sh.period_reporting_cell}")
            if sh.period_campaign_cell:
                lines.append(f"  other_period_cell: {sh.period_campaign_cell}")
            if sh.days_count_cell:
                lines.append(f"  count_cell: {sh.days_count_cell}")
            if sh.dates_are_formulas:
                lines.append("  dates_are_formulas: true")
            if sh.role == "source":
                lines.append(f"  data_rows: {sh.data_rows}")
    return "\n".join(lines)


def extract_date_values(
    path: Path | str,
    *,
    sheet: str,
    date_column: str,
    value_column: str,
) -> dict[str, int | float]:
    """Load one sheet and extract date-to-value pairs from explicit columns."""
    import openpyxl
    from openpyxl.utils.cell import column_index_from_string

    p = Path(path)
    wb = openpyxl.load_workbook(str(p), data_only=True)
    try:
        if sheet not in wb.sheetnames:
            raise ValueError(f"Sheet {sheet!r} not in {p.name}")
        ws = wb[sheet]
        max_row = int(ws.max_row or 0)
        max_col = int(ws.max_column or 0)
        grid = _read_grid(ws, max_row=min(max_row, 500), max_col=min(max_col, 40))
        date_idx = column_index_from_string(date_column.upper())
        value_idx = column_index_from_string(value_column.upper())
        # Prefer detected header row; fall back to row 1. The caller-supplied
        # Caller-supplied date/value columns are authoritative,
        # so the guessed column indices are intentionally unused.
        header_row, _guessed_date_col, _guessed_value_col = _guess_source_header(grid)
        if header_row is None:
            header_row = 1
        return _extract_date_values(grid, header_row, date_idx, value_idx)
    finally:
        wb.close()


def extract_key_values(
    path: Path | str,
    *,
    sheet: str,
    key_column: str,
    value_column: str,
) -> dict[str, int | float]:
    """Load one sheet and extract label-to-value pairs from explicit columns."""
    import openpyxl
    from openpyxl.utils.cell import column_index_from_string

    p = Path(path)
    wb = openpyxl.load_workbook(str(p), data_only=True)
    try:
        if sheet not in wb.sheetnames:
            raise ValueError(f"Sheet {sheet!r} not in {p.name}")
        ws = wb[sheet]
        max_row = int(ws.max_row or 0)
        max_col = int(ws.max_column or 0)
        grid = _read_grid(ws, max_row=min(max_row, 500), max_col=min(max_col, 40))
        key_idx = column_index_from_string(key_column.upper())
        value_idx = column_index_from_string(value_column.upper())
        # Caller-supplied key_column/value_column are authoritative (region-
        # mapping design); guessed column indices are intentionally unused.
        header_row, _guessed_key_col, _guessed_value_col = _guess_key_source_header(grid)
        if header_row is None:
            header_row = 1
        return _extract_key_values(grid, header_row, key_idx, value_idx)
    finally:
        wb.close()


def _brief_file(path: Path) -> FileBrief:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=True)
    # CR-13: lazy-load the formula workbook. It is only needed when a sheet
    # has a template_date_col (to detect =F16+1 / =LEFT(...) date formulas).
    # Source files never trigger that path, so we avoid a second full xlsx
    # parse for every attached source. The loader memoizes one load per file.
    wb_formulas_holder: list[Any] = []  # [wb_formulas] once loaded

    def _load_formula_workbook() -> Any:
        if not wb_formulas_holder:
            wb_formulas_holder.append(openpyxl.load_workbook(str(path), data_only=False))
        return wb_formulas_holder[0]

    try:
        sheets: list[SheetBrief] = []
        for name in wb.sheetnames:
            ws = wb[name]
            sheets.append(_brief_sheet(name, ws, _load_formula_workbook))
        # Role at file level: if any template sheet → file is template-ish
        return FileBrief(path=str(path), name=path.name, sheets=sheets)
    finally:
        wb.close()
        if wb_formulas_holder:
            wb_formulas_holder[0].close()


def _brief_sheet(
    name: str,
    ws: Any,
    formula_workbook_loader: Callable[[], Any],
) -> SheetBrief:
    from openpyxl.utils.cell import get_column_letter

    max_row = int(ws.max_row or 0)
    max_col = int(ws.max_column or 0)
    grid = _read_grid(ws, max_row=min(max_row, 80), max_col=min(max_col, 20))
    header_row, date_col_idx, value_col_idx = _guess_source_header(grid)
    key_header_row, key_col_idx, key_value_col_idx = _guess_key_source_header(grid)

    period_reporting = _find_label_value_cell(
        grid, ("период отчетности", "период отчётности", "reporting period")
    )
    period_campaign = _find_label_value_cell(grid, ("период кампании", "campaign period"))
    days_count = _find_label_value_cell(grid, ("количество дней", "day count"))

    fact_col = _find_header_col(grid, _FACT_HEADERS, search_rows=50)
    plan_col = _find_header_col(grid, _PLAN_HEADERS, search_rows=50)
    # Prefer an explicit date/period header, otherwise infer from parseable dates.
    template_date_col = _find_exact_header_col(grid, "период", search_rows=50)
    if template_date_col is None:
        template_date_col = _guess_daily_date_column(grid)
    template_key_col = _find_header_col(grid, _KEY_HEADERS, search_rows=50)

    dates_are_formulas = False
    if template_date_col:
        # CR-13: lazy-load — the formula workbook is only parsed when at least
        # one sheet reaches this branch (i.e. has a template_date_col). Source
        # files never trigger this load.
        ws_formulas = formula_workbook_loader()[name]
        col_idx = _letter_to_idx(template_date_col)
        for r in range(1, min(max_row, 80) + 1):
            raw = ws_formulas.cell(row=r, column=col_idx).value
            if isinstance(raw, str) and raw.startswith("="):
                dates_are_formulas = True
                break

    role = "other"
    match_mode = "date"
    date_values: dict[str, int | float] = {}
    key_values: dict[str, int | float] = {}
    date_col_letter = get_column_letter(date_col_idx) if date_col_idx else None
    value_col_letter = get_column_letter(value_col_idx) if value_col_idx else None

    if header_row and date_col_idx and value_col_idx:
        date_values = _extract_date_values(grid, header_row, date_col_idx, value_col_idx)
        if date_values:
            role = "source"
            match_mode = "date"

    if role != "source" and key_header_row and key_col_idx and key_value_col_idx:
        key_values = _extract_key_values(grid, key_header_row, key_col_idx, key_value_col_idx)
        if key_values:
            role = "source"
            match_mode = "key"
            header_row = key_header_row
            date_col_letter = get_column_letter(key_col_idx)
            value_col_letter = get_column_letter(key_value_col_idx)

    # A structurally valid header with empty target rows is a template. Do not
    # guess any missing columns: the discovered coordinates remain authoritative.
    if role != "source" and header_row and date_col_idx and value_col_idx:
        role = "template"
        match_mode = "date"
        template_date_col = get_column_letter(date_col_idx)
        if fact_col is None:
            fact_col = get_column_letter(value_col_idx)
    elif role != "source" and key_header_row and key_col_idx and key_value_col_idx:
        role = "template"
        match_mode = "key"
        template_date_col = get_column_letter(key_col_idx)
        if fact_col is None:
            fact_col = get_column_letter(key_value_col_idx)

    if (period_campaign or period_reporting or days_count or fact_col) and (
        period_campaign or fact_col
    ):
        role = "template"
        if template_key_col and not dates_are_formulas and template_date_col is None:
            match_mode = "key"
            template_date_col = template_key_col

    dates = sorted(date_values)

    return SheetBrief(
        name=name,
        max_row=max_row,
        max_col=max_col,
        header_row=header_row,
        date_column=date_col_letter if role == "source" else template_date_col,
        value_column=value_col_letter if role == "source" else fact_col,
        fact_column=fact_col,
        plan_column=plan_col,
        period_reporting_cell=period_reporting,
        period_campaign_cell=period_campaign,
        days_count_cell=days_count,
        dates_are_formulas=dates_are_formulas,
        role=role,
        match_mode=match_mode,
        data_rows=len(key_values) if match_mode == "key" else len(date_values),
        date_min=dates[0] if dates else None,
        date_max=dates[-1] if dates else None,
    )


def _read_grid(ws: Any, *, max_row: int, max_col: int) -> list[list[Any]]:
    grid: list[list[Any]] = []
    for r in range(1, max_row + 1):
        row = [ws.cell(row=r, column=c).value for c in range(1, max_col + 1)]
        grid.append(row)
    return grid


def _guess_source_header(grid: list[list[Any]]) -> tuple[int | None, int | None, int | None]:
    """Return (header_row_1based, date_col_1based, value_col_1based)."""
    for r_idx, row in enumerate(grid[:50]):
        date_col = None
        value_col = None
        for c_idx, cell in enumerate(row):
            if cell is None:
                continue
            text = str(cell).strip().casefold()
            if not text:
                continue
            if (
                date_col is None
                and len(text) < 40
                and any(h == text or h in text for h in _DATE_HEADERS)
            ):
                date_col = c_idx + 1
            if (
                value_col is None
                and len(text) < 40
                and "факт" not in text
                and any(h == text or text.startswith(h) for h in _VALUE_HEADERS)
            ):
                value_col = c_idx + 1
        if date_col and value_col:
            return r_idx + 1, date_col, value_col
    return None, None, None


def _guess_key_source_header(grid: list[list[Any]]) -> tuple[int | None, int | None, int | None]:
    """Return (header_row_1based, key_col_1based, value_col_1based)."""
    for r_idx, row in enumerate(grid[:50]):
        key_col = None
        value_col = None
        for c_idx, cell in enumerate(row):
            if cell is None:
                continue
            text = str(cell).strip().casefold()
            if not text:
                continue
            if (
                key_col is None
                and len(text) < 40
                and any(h == text or h in text for h in _KEY_HEADERS)
            ):
                key_col = c_idx + 1
            if (
                value_col is None
                and len(text) < 40
                and "факт" not in text
                and any(h == text or text.startswith(h) for h in _VALUE_HEADERS)
            ):
                value_col = c_idx + 1
        if key_col and value_col:
            return r_idx + 1, key_col, value_col
    return None, None, None


def _extract_key_values(
    grid: list[list[Any]],
    header_row: int,
    key_col: int,
    value_col: int,
) -> dict[str, int | float]:
    out: dict[str, int | float] = {}
    for r_idx in range(header_row, len(grid)):
        row = grid[r_idx]
        if key_col - 1 >= len(row) or value_col - 1 >= len(row):
            continue
        k_raw = row[key_col - 1]
        v_raw = row[value_col - 1]
        if k_raw is None:
            continue
        key = str(k_raw).strip()
        if not key:
            continue
        folded = key.casefold()
        if any(m in folded for m in _SKIP_ROW_MARKERS):
            continue
        if folded in _KEY_HEADERS:
            continue
        # Skip pure date rows — those belong to date sources.
        if _parse_date(k_raw) is not None:
            continue
        num = _parse_number(v_raw)
        if num is None:
            continue
        out[key] = float(out.get(key, 0)) + float(num)
    return {k: int(v) if float(v).is_integer() else v for k, v in out.items()}


def _extract_date_values(
    grid: list[list[Any]],
    header_row: int,
    date_col: int,
    value_col: int,
) -> dict[str, int | float]:
    out: dict[str, int | float] = {}
    for r_idx in range(header_row, len(grid)):
        row = grid[r_idx]
        if date_col - 1 >= len(row) or value_col - 1 >= len(row):
            continue
        d_raw = row[date_col - 1]
        v_raw = row[value_col - 1]
        if d_raw is None:
            continue
        d_text = str(d_raw).strip().casefold()
        if any(m in d_text for m in _SKIP_ROW_MARKERS):
            continue
        parsed = _parse_date(d_raw)
        if parsed is None:
            continue
        num = _parse_number(v_raw)
        if num is None:
            continue
        key = parsed.isoformat()
        out[key] = float(out.get(key, 0)) + float(num)
    # Prefer ints
    return {k: int(v) if float(v).is_integer() else v for k, v in out.items()}


def _find_label_value_cell(grid: list[list[Any]], labels: tuple[str, ...]) -> str | None:
    """Find label in col E-ish and return adjacent value cell address (usually F)."""
    from openpyxl.utils.cell import get_column_letter

    for r_idx, row in enumerate(grid[:20]):
        for c_idx, cell in enumerate(row):
            if cell is None:
                continue
            text = str(cell).strip().casefold()
            if text in labels:
                # value typically next non-empty to the right
                for dc in range(c_idx + 1, min(len(row), c_idx + 3)):
                    if row[dc] is not None and str(row[dc]).strip() != "":
                        return f"{get_column_letter(dc + 1)}{r_idx + 1}"
                return f"{get_column_letter(c_idx + 2)}{r_idx + 1}"
    return None


def _find_header_col(
    grid: list[list[Any]], headers: tuple[str, ...], *, search_rows: int
) -> str | None:
    from openpyxl.utils.cell import get_column_letter

    for _r_idx, row in enumerate(grid[:search_rows]):
        for c_idx, cell in enumerate(row):
            if cell is None:
                continue
            text = str(cell).strip().casefold()
            for h in headers:
                if text == h or text.rstrip() == h or text.startswith(h):
                    return get_column_letter(c_idx + 1)
    return None


def _find_exact_header_col(grid: list[list[Any]], header: str, *, search_rows: int) -> str | None:
    from openpyxl.utils.cell import get_column_letter

    needle = header.casefold()
    for row in grid[:search_rows]:
        for c_idx, cell in enumerate(row):
            if cell is None:
                continue
            text = str(cell).strip().casefold()
            if text == needle or text.rstrip() == needle:
                return get_column_letter(c_idx + 1)
    return None


def _guess_daily_date_column(grid: list[list[Any]]) -> str | None:
    """Pick the column with the most parseable dates in the bounded scan."""
    from openpyxl.utils.cell import get_column_letter

    if not grid:
        return None
    best_col: int | None = None
    best_count = 0
    max_col = max((len(r) for r in grid), default=0)
    for c_idx in range(max_col):
        count = 0
        for r_idx in range(min(len(grid), 80)):
            if c_idx >= len(grid[r_idx]):
                continue
            if _parse_date(grid[r_idx][c_idx]) is not None:
                count += 1
        if count > best_count:
            best_count = count
            best_col = c_idx + 1
    if best_col is not None and best_count >= 3:
        return get_column_letter(best_col)
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    # Excel serial sometimes as int
    return None


def _parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(" ", "").replace(",", ".")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _letter_to_idx(letter: str) -> int:
    n = 0
    for ch in letter.upper():
        if not ("A" <= ch <= "Z"):
            continue
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n
