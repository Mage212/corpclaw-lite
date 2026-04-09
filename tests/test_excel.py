"""Tests for NormalizeExcelTool."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.extensions.tools.builtin.excel import NormalizeExcelTool, _clean_header


@pytest.fixture
def tool() -> NormalizeExcelTool:
    return NormalizeExcelTool()


def _create_xlsx(path: Path, headers: list[str], rows: list[list[object]]) -> Path:
    """Helper to create a .xlsx file with openpyxl."""
    import openpyxl  # type: ignore[import-untyped]

    wb = openpyxl.Workbook()
    ws = wb.active
    for col_idx, h in enumerate(headers, 1):
        ws.cell(row=1, column=col_idx, value=h)  # type: ignore[union-attr]
    for row_idx, row in enumerate(rows, 2):
        for col_idx, val in enumerate(row, 1):
            ws.cell(row=row_idx, column=col_idx, value=val)  # type: ignore[union-attr]
    wb.save(str(path))
    return path


def test_clean_header() -> None:
    assert _clean_header("First Name ") == "first_name"
    assert _clean_header("  LAST   NAME  ") == "last_name"
    assert _clean_header("email") == "email"


@pytest.mark.asyncio
async def test_normalize_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: NormalizeExcelTool
) -> None:
    monkeypatch.chdir(tmp_path)
    _create_xlsx(tmp_path / "data.xlsx", ["First Name", " AGE "], [["Alice", 30]])
    result = await tool.execute(path="data.xlsx")
    assert "Headers normalized" in result

    import openpyxl  # type: ignore[import-untyped]

    wb = openpyxl.load_workbook(str(tmp_path / "data_normalized.xlsx"))
    ws = wb.active
    assert ws.cell(row=1, column=1).value == "first_name"  # type: ignore[union-attr]
    assert ws.cell(row=1, column=2).value == "age"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_remove_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: NormalizeExcelTool
) -> None:
    monkeypatch.chdir(tmp_path)
    _create_xlsx(
        tmp_path / "data.xlsx",
        ["name", "age"],
        [["Alice", 30], ["Alice", 30], ["Bob", 25]],
    )
    result = await tool.execute(path="data.xlsx", normalize_headers=False)
    assert "Duplicates removed: 1" in result


@pytest.mark.asyncio
async def test_remove_empty_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: NormalizeExcelTool
) -> None:
    monkeypatch.chdir(tmp_path)
    _create_xlsx(
        tmp_path / "data.xlsx",
        ["name", "age"],
        [["Alice", 30], [None, None], ["Bob", 25]],
    )
    result = await tool.execute(path="data.xlsx", normalize_headers=False)
    assert "Empty rows removed: 1" in result


@pytest.mark.asyncio
async def test_default_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: NormalizeExcelTool
) -> None:
    monkeypatch.chdir(tmp_path)
    _create_xlsx(tmp_path / "report.xlsx", ["a"], [["x"]])
    result = await tool.execute(path="report.xlsx")
    assert "report_normalized.xlsx" in result
    assert (tmp_path / "report_normalized.xlsx").exists()


@pytest.mark.asyncio
async def test_custom_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: NormalizeExcelTool
) -> None:
    monkeypatch.chdir(tmp_path)
    _create_xlsx(tmp_path / "data.xlsx", ["a"], [["x"]])
    result = await tool.execute(path="data.xlsx", output_path="clean.xlsx")
    assert "clean.xlsx" in result
    assert (tmp_path / "clean.xlsx").exists()


@pytest.mark.asyncio
async def test_file_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: NormalizeExcelTool
) -> None:
    monkeypatch.chdir(tmp_path)
    result = await tool.execute(path="missing.xlsx")
    assert "Error" in result
    assert "does not exist" in result


@pytest.mark.asyncio
async def test_non_xlsx_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: NormalizeExcelTool
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data.csv").write_text("a,b\n1,2\n")
    result = await tool.execute(path="data.csv")
    assert "Error" in result
    assert ".xlsx" in result


@pytest.mark.asyncio
async def test_path_traversal_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: NormalizeExcelTool
) -> None:
    monkeypatch.chdir(tmp_path)
    result = await tool.execute(path="../secret.xlsx")
    assert "Error" in result
