"""Main-agent tool for deterministic source-to-target workbook filling."""

from __future__ import annotations

import json
from typing import Any, cast

from corpclaw_lite.agent.fill_plan import (
    FillPeriodPlan,
    FillPlan,
    FillSheetPlan,
    FillSourceRef,
    apply_fill_plan,
    parse_fill_plan,
)
from corpclaw_lite.agent.workspace_context import get_workspace_root
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.security.path_validator import resolve_and_validate_path

__all__ = ["ApplyFillPlanTool"]


class ApplyFillPlanTool(Tool):
    """Apply an explicit source-to-target workbook fill plan."""

    name = "apply_fill_plan"
    description = (
        "Apply an explicit FillPlan to an Excel template. "
        "Pass plan with template, output_path, cutoff, and sheets[] "
        "(sheet, date_column, value_column, sources[{file,sheet,date_column,value_column}], "
        "optional match_mode=date|key, period{cell,start_mode,format}, aggregate=sum|first). "
        "Use match_mode=key when date_column contains labels rather than dates. "
        "Do not pass values{}, period_cell, or period_value; Python extracts source values."
    )
    params = [
        ToolParam(
            name="plan",
            type="string",
            description=(
                "Region FillPlan as JSON object or string: "
                '{"template":"template.xlsx","output_path":"target_report.xlsx",'
                '"cutoff":"2030-03-10","sheets":[{"sheet":"Daily","match_mode":"date",'
                '"date_column":"A","value_column":"B","aggregate":"sum",'
                '"period":{"cell":"D2","start_mode":"source_min","format":"iso"},'
                '"sources":[{"file":"source.xlsx","sheet":"Data",'
                '"date_column":"A","value_column":"B"}]}]}'
            ),
        ),
    ]
    risk_level = RiskLevel.MEDIUM
    parallel_safe = False
    terminal = False

    async def execute(self, **kwargs: Any) -> str:
        raw = kwargs.get("plan")
        if raw is None:
            return "Error: 'plan' parameter is required."
        try:
            if isinstance(raw, str):
                plan = parse_fill_plan(raw)
            elif isinstance(raw, dict):
                plan = parse_fill_plan(cast(dict[str, Any], raw))
            else:
                return "Error: 'plan' must be a JSON object or JSON string."
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            return f"Error: invalid FillPlan: {exc}"

        try:
            template = resolve_and_validate_path(plan.template)
            output = resolve_and_validate_path(plan.output_path)
        except PermissionError as exc:
            return f"Error: {exc}"

        if not template.is_file():
            return f"Error: template '{plan.template}' does not exist."

        bound_sources_sheets: list[FillSheetPlan] = []
        for s in plan.sheets:
            bound_sources: list[FillSourceRef] = []
            for src in s.sources:
                try:
                    src_path = resolve_and_validate_path(src.file)
                except PermissionError as exc:
                    return f"Error: {exc}"
                bound_sources.append(
                    FillSourceRef(
                        file=str(src_path),
                        sheet=src.sheet,
                        date_column=src.date_column,
                        value_column=src.value_column,
                    )
                )
            bound_sources_sheets.append(
                FillSheetPlan(
                    sheet=s.sheet,
                    date_column=s.date_column,
                    value_column=s.value_column,
                    sources=bound_sources,
                    aggregate=s.aggregate,
                    period=(
                        FillPeriodPlan(
                            cell=s.period.cell,
                            start_mode=s.period.start_mode,
                            format=s.period.format,
                        )
                        if s.period
                        else None
                    ),
                    period_cell=s.period_cell,
                    only_empty=s.only_empty,
                    match_mode=s.match_mode,
                )
            )

        bound = FillPlan(
            template=str(template),
            output_path=str(output),
            sheets=bound_sources_sheets,
            cutoff=plan.cutoff,
        )
        workspace = get_workspace_root() or template.parent
        try:
            results = apply_fill_plan(bound, workspace=workspace)
        except ValueError as exc:
            return f"Error: {exc}"
        if not results:
            return "Error: apply_fill_plan produced no results."
        joined = "\n".join(results)
        if any(r.split("] ", 1)[-1].startswith("Error:") for r in results):
            return joined
        return f"SUCCESS: apply_fill_plan finished.\n{joined}"
