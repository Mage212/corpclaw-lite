from __future__ import annotations

from pathlib import Path


def test_data_prompts_explain_aggregate_before_chart() -> None:
    root = Path(__file__).resolve().parents[1]
    data_agent = (root / "config/bootstrap/subagents/data-agent.md").read_text(encoding="utf-8")
    data_skill = (root / "skills/data_analyst.md").read_text(encoding="utf-8")

    assert "table_query output_path" in data_agent
    assert "chart_generate" in data_agent
    assert "output_path" in data_skill
    assert "chart_generate" in data_skill


def test_excel_fill_prompts_describe_copy_default() -> None:
    root = Path(__file__).resolve().parents[1]
    document_agent = (root / "config/bootstrap/subagents/document.md").read_text(encoding="utf-8")
    filler_skill = (root / "skills/excel_filler.md").read_text(encoding="utf-8")

    assert "_filled.xlsx" in document_agent
    assert "in_place=true" in document_agent
    assert "_filled.xlsx" in filler_skill
