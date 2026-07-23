"""FillPlan parse + resolve (sources→values) + apply via fill_by_date."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

__all__ = [
    "FillPlan",
    "FillPeriodPlan",
    "FillSheetPlan",
    "FillSourceRef",
    "apply_fill_plan",
    "parse_fill_plan",
    "parse_fill_plan_from_text",
    "resolve_fill_plan",
]


def _empty_sources() -> list[FillSourceRef]:
    return []


def _empty_values() -> dict[str, Any]:
    return {}


@dataclass(slots=True)
class FillSourceRef:
    """Where to read date→value pairs (model mapping; Python extracts numbers)."""

    file: str
    sheet: str
    date_column: str
    value_column: str


@dataclass(slots=True)
class FillPeriodPlan:
    """Explicit policy for updating a reporting-period cell."""

    cell: str
    start_mode: str = "preserve"  # preserve | source_min | month_start
    format: str = "preserve"  # preserve | dmy | iso


@dataclass(slots=True)
class FillSheetPlan:
    sheet: str
    date_column: str  # date column, or key/label column when match_mode=key
    value_column: str
    sources: list[FillSourceRef] = field(default_factory=_empty_sources)
    aggregate: str = "sum"  # sum | first
    values: dict[str, Any] = field(default_factory=_empty_values)  # resolved-only
    period: FillPeriodPlan | None = None
    # Resolved-only low-level values passed to the workbook writer.
    period_cell: str | None = None
    period_value: Any = None
    only_empty: bool = True
    match_mode: str = "date"  # date | key (arbitrary labels)


@dataclass(slots=True)
class FillPlan:
    template: str
    output_path: str
    sheets: list[FillSheetPlan]
    cutoff: str | None = None  # ISO or DD.MM.YYYY inclusive


def parse_fill_plan(raw: dict[str, Any] | str) -> FillPlan:
    """Parse an explicit source-to-target FillPlan from dict or JSON string.

    Model-supplied ``values`` are rejected — numbers are resolved from ``sources``.
    """
    parsed: object = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, dict):
        raise ValueError("FillPlan must be a JSON object")
    data = cast(dict[str, Any], parsed)
    template = str(data.get("template") or "").strip()
    if not template:
        raise ValueError("FillPlan.template is required")
    output_path = str(data.get("output_path") or "target_report.xlsx").strip()
    cutoff_raw = data.get("cutoff")
    cutoff = str(cutoff_raw).strip() if cutoff_raw not in (None, "") else None

    sheets_raw_obj: object = data.get("sheets")
    if not isinstance(sheets_raw_obj, list) or not sheets_raw_obj:
        raise ValueError("FillPlan.sheets must be a non-empty list")
    sheets_raw = cast(list[object], sheets_raw_obj)
    sheets: list[FillSheetPlan] = []
    for item in sheets_raw:
        if not isinstance(item, dict):
            raise ValueError("Each FillPlan.sheets item must be an object")
        item_d = cast(dict[str, Any], item)
        if item_d.get("values") not in (None, {}, ""):
            raise ValueError(
                "FillPlan.sheets[].values is not allowed — pass sources[] and cutoff; "
                "Python extracts numbers"
            )
        sheet = str(item_d.get("sheet") or "").strip()
        date_column = str(item_d.get("date_column") or "").strip()
        value_column = str(item_d.get("value_column") or "").strip()
        if not sheet or not date_column or not value_column:
            raise ValueError("sheet, date_column, value_column are required per sheet")

        sources_raw_obj: object = item_d.get("sources")
        if not isinstance(sources_raw_obj, list) or not sources_raw_obj:
            raise ValueError("sources must be a non-empty list per sheet")
        sources_raw = cast(list[object], sources_raw_obj)
        sources: list[FillSourceRef] = []
        for src_item in sources_raw:
            if not isinstance(src_item, dict):
                raise ValueError("Each sources item must be an object")
            src_d = cast(dict[str, Any], src_item)
            src_file = str(src_d.get("file") or "").strip()
            src_sheet = str(src_d.get("sheet") or "").strip()
            src_date = str(src_d.get("date_column") or "").strip()
            src_value = str(src_d.get("value_column") or "").strip()
            if not src_file or not src_sheet or not src_date or not src_value:
                raise ValueError("sources[] requires file, sheet, date_column, value_column")
            sources.append(
                FillSourceRef(
                    file=src_file,
                    sheet=src_sheet,
                    date_column=src_date,
                    value_column=src_value,
                )
            )

        aggregate = str(item_d.get("aggregate") or ("sum" if len(sources) > 1 else "first"))
        aggregate = aggregate.strip().casefold()
        if aggregate not in {"sum", "first"}:
            raise ValueError("aggregate must be 'sum' or 'first'")
        match_mode = str(item_d.get("match_mode") or "date").strip().casefold()
        if match_mode not in {"date", "key"}:
            raise ValueError("match_mode must be 'date' or 'key'")

        if item_d.get("period_cell") not in (None, ""):
            raise ValueError("FillPlan.sheets[].period_cell is obsolete — use period{...}")
        if item_d.get("period_value") not in (None, ""):
            raise ValueError("FillPlan.sheets[].period_value is not allowed")

        period: FillPeriodPlan | None = None
        period_raw = item_d.get("period")
        if period_raw not in (None, ""):
            if not isinstance(period_raw, dict):
                raise ValueError("period must be an object")
            period_d = cast(dict[str, Any], period_raw)
            cell = str(period_d.get("cell") or "").strip()
            start_mode = str(period_d.get("start_mode") or "preserve").strip().casefold()
            period_format = str(period_d.get("format") or "preserve").strip().casefold()
            if not cell:
                raise ValueError("period.cell is required")
            if start_mode not in {"preserve", "source_min", "month_start"}:
                raise ValueError("period.start_mode must be preserve, source_min, or month_start")
            if period_format not in {"preserve", "dmy", "iso"}:
                raise ValueError("period.format must be preserve, dmy, or iso")
            if not cutoff:
                raise ValueError("FillPlan.cutoff is required when period is present")
            if match_mode == "key" and start_mode == "source_min":
                raise ValueError("period.start_mode=source_min is not valid for key matching")
            period = FillPeriodPlan(cell=cell, start_mode=start_mode, format=period_format)
        sheets.append(
            FillSheetPlan(
                sheet=sheet,
                date_column=date_column,
                value_column=value_column,
                sources=sources,
                aggregate=aggregate,
                period=period,
                period_value=None,
                only_empty=bool(item_d.get("only_empty", True)),
                match_mode=match_mode,
            )
        )
    return FillPlan(
        template=template,
        output_path=output_path,
        sheets=sheets,
        cutoff=cutoff,
    )


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def parse_fill_plan_from_text(text: str) -> FillPlan:
    """Extract FillPlan JSON from model text (fenced or raw object)."""
    text = text.strip()
    m = _JSON_FENCE.search(text)
    if m:
        return parse_fill_plan(m.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return parse_fill_plan(text[start : end + 1])
    raise ValueError("No FillPlan JSON object found in model response")


def _parse_cutoff(raw: str | None) -> date | None:
    if not raw:
        return None
    text = raw.strip()
    # CR-20: %d.%m.%y (2-digit year) dropped — footgun for reporting tools
    # ("12.07.99" → 1999 or 2099 depending on platform). Require explicit
    # 4-digit years via ISO or DD.MM.YYYY.
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Allow trailing time / ISO with T
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    raise ValueError(f"Invalid cutoff date: {raw!r}")


_PERIOD_DATE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{2}[./]\d{2}[./]\d{4})")


def _period_start_and_format(value: Any) -> tuple[date | None, str | None]:
    """Return the first date and recognized formatting style from a cell."""
    if isinstance(value, datetime):
        return value.date(), "iso"
    if isinstance(value, date):
        return value, "iso"
    match = _PERIOD_DATE.search(str(value or ""))
    if not match:
        return None, None
    text = match.group(1)
    if "-" in text:
        return datetime.strptime(text, "%Y-%m-%d").date(), "iso"
    fmt = "slash" if "/" in text else "dmy"
    return datetime.strptime(text, "%d/%m/%Y" if fmt == "slash" else "%d.%m.%Y").date(), fmt


def _format_period(start: date, cutoff: date, style: str) -> str:
    if style == "iso":
        return f"{start.isoformat()} - {cutoff.isoformat()}"
    separator = "/" if style == "slash" else "."
    fmt = f"%d{separator}%m{separator}%Y"
    return f"{start.strftime(fmt)} - {cutoff.strftime(fmt)}"


def _resolve_period_value(
    sheet: FillSheetPlan,
    *,
    cutoff: date,
    source_min: date | None,
    template: Path,
) -> str | None:
    policy = sheet.period
    if policy is None:
        return None

    import openpyxl

    wb = openpyxl.load_workbook(str(template), data_only=False, read_only=True)
    try:
        if sheet.sheet not in wb.sheetnames:
            raise ValueError(f"Sheet {sheet.sheet!r} not in {template.name}")
        current = wb[sheet.sheet][policy.cell].value
    finally:
        wb.close()

    current_start, current_style = _period_start_and_format(current)
    if policy.start_mode == "preserve":
        if current_start is None:
            raise ValueError(
                f"Cannot preserve period start in {sheet.sheet!r}!{policy.cell}: cell has no date"
            )
        start = current_start
    elif policy.start_mode == "source_min":
        if source_min is None:
            raise ValueError(f"Cannot derive source_min period for sheet {sheet.sheet!r}")
        start = source_min
    else:
        start = date(cutoff.year, cutoff.month, 1)

    style = current_style if policy.format == "preserve" else policy.format
    if style is None:
        raise ValueError(
            f"Cannot preserve period format in {sheet.sheet!r}!{policy.cell}; use dmy or iso"
        )
    return _format_period(start, cutoff, style)


def resolve_fill_plan(plan: FillPlan, *, workspace: Path | None = None) -> FillPlan:
    """Read sources and fill ``values`` / ``period_value`` for each sheet."""
    from corpclaw_lite.agent.workbook_brief import (
        extract_date_values,
        extract_key_values,
    )

    base = workspace or Path.cwd()
    template = Path(plan.template)
    if not template.is_absolute():
        template = (base / template).resolve()
    cutoff = _parse_cutoff(plan.cutoff)
    resolved_sheets: list[FillSheetPlan] = []
    for sheet in plan.sheets:
        merged: dict[str, float] = {}
        mode = sheet.match_mode.casefold()
        for src in sheet.sources:
            path = Path(src.file)
            if not path.is_absolute():
                # CR-7: validate against the workspace boundary when workspace
                # is explicit. Absolute paths are assumed pre-validated by the
                # caller (ApplyFillPlanTool resolves them via resolve_and_validate_path).
                if workspace is not None:
                    from corpclaw_lite.security.path_validator import resolve_and_validate_path

                    try:
                        path = resolve_and_validate_path(src.file, workspace_root=base)
                    except PermissionError as exc:
                        raise ValueError(f"Source path rejected for {src.file!r}: {exc}") from exc
                else:
                    path = (base / path).resolve()
            if not path.is_file():
                raise ValueError(f"Source file not found: {src.file}")
            if mode == "key":
                extracted = extract_key_values(
                    path,
                    sheet=src.sheet,
                    key_column=src.date_column,
                    value_column=src.value_column,
                )
            else:
                extracted = extract_date_values(
                    path,
                    sheet=src.sheet,
                    date_column=src.date_column,
                    value_column=src.value_column,
                )
            for item_key, num in extracted.items():
                if mode == "date":
                    day = date.fromisoformat(item_key)
                    if cutoff is not None and day > cutoff:
                        continue
                if sheet.aggregate == "first":
                    if item_key not in merged:
                        merged[item_key] = float(num)
                else:
                    merged[item_key] = float(merged.get(item_key, 0)) + float(num)
        if not merged:
            kind = "key→value" if mode == "key" else "date→value"
            raise ValueError(
                f"No {kind} pairs resolved for sheet {sheet.sheet!r} "
                f"(sources={len(sheet.sources)}, cutoff={plan.cutoff!r})"
            )
        clean: dict[str, Any] = {
            k: int(v) if float(v).is_integer() else v for k, v in sorted(merged.items())
        }
        source_min = (
            min((date.fromisoformat(k) for k in merged), default=None) if mode == "date" else None
        )
        period_value = sheet.period_value
        period_cell = sheet.period_cell
        if sheet.period is not None:
            if cutoff is None:
                raise ValueError("FillPlan.cutoff is required when period is present")
            period_cell = sheet.period.cell
            period_value = _resolve_period_value(
                sheet,
                cutoff=cutoff,
                source_min=source_min,
                template=template,
            )
        resolved_sheets.append(
            FillSheetPlan(
                sheet=sheet.sheet,
                date_column=sheet.date_column,
                value_column=sheet.value_column,
                sources=list(sheet.sources),
                aggregate=sheet.aggregate,
                values=clean,
                period=sheet.period,
                period_cell=period_cell,
                period_value=period_value,
                only_empty=sheet.only_empty,
                match_mode=sheet.match_mode,
            )
        )
    return FillPlan(
        template=plan.template,
        output_path=plan.output_path,
        sheets=resolved_sheets,
        cutoff=plan.cutoff,
    )


def _is_fill_result_error(result: str) -> bool:
    """True when a filler (fill_by_date / fill_by_key) result indicates failure.

    CR-19: centralizes the error-detection pattern so apply_fill_plan does not
    depend on an inline string-prefix check that may drift. Fillers emit one
    of: ``SUCCESS:``, ``PARTIAL:``, ``NOOP:``, ``Dry-run``, ``Error: ...``,
    or the edge-case ``Error saving file: ...``. Only ``Error*`` is a hard
    failure that should stop the multi-sheet loop.
    """
    return result.startswith("Error")


def apply_fill_plan(
    plan: FillPlan,
    *,
    workspace: Path | None = None,
) -> list[str]:
    """Resolve sources→values (if needed) then apply via fill_by_date / fill_by_key.

    Returns a list of per-sheet result strings from the workbook tool.
    """
    from corpclaw_lite.extensions.tools.builtin.excel_workbook import fill_by_date, fill_by_key

    base = workspace or Path.cwd()
    if any(s.sources for s in plan.sheets):
        plan = resolve_fill_plan(plan, workspace=base)

    template = Path(plan.template)
    if not template.is_absolute():
        template = (base / template).resolve()
    output = Path(plan.output_path)
    if not output.is_absolute():
        output = (base / output).resolve()

    results: list[str] = []
    current_input = template
    for idx, sheet in enumerate(plan.sheets):
        if not sheet.values:
            # CR-6: continue, not break — every sheet must report so the caller
            # (and the main agent) sees which sheets failed, not just the first.
            results.append(f"[{sheet.sheet}] Error: no values to fill (resolve failed)")
            continue
        out = output
        in_place_src = current_input if idx == 0 else output
        filler = fill_by_key if sheet.match_mode.casefold() == "key" else fill_by_date
        result = filler(
            in_place_src,
            sheet.sheet,
            sheet.date_column,
            sheet.value_column,
            sheet.values,
            out,
            period_cell=sheet.period_cell,
            period_value=sheet.period_value,
            only_empty=sheet.only_empty,
            dry_run=False,
        )
        results.append(f"[{sheet.sheet}] {result}")
        current_input = out
        if _is_fill_result_error(result):
            break
    return results
