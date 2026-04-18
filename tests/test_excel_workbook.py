"""Tests for ExcelWorkbookTool."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest
from openpyxl.styles import PatternFill

from corpclaw_lite.extensions.tools.builtin.excel_workbook import ExcelWorkbookTool


@pytest.fixture
def tool() -> ExcelWorkbookTool:
    return ExcelWorkbookTool()


def _create_basic_xlsx(
    path: Path,
    headers: list[str],
    rows: list[list[object]],
) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)
    for row_idx, row in enumerate(rows, 2):
        for col, val in enumerate(row, 1):
            ws.cell(row=row_idx, column=col, value=val)
    wb.save(str(path))
    return path


class TestExcelWorkbookRead:
    @pytest.mark.asyncio
    async def test_read_default(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read without specifying cells returns first 20 rows."""
        monkeypatch.chdir(tmp_path)
        headers = ["Name", "Age", "City"]
        rows = [[f"user{i}", 20 + i, f"city{i}"] for i in range(1, 25)]
        _create_basic_xlsx(tmp_path / "data.xlsx", headers, rows)

        result = await tool.execute(path="data.xlsx", action="read")
        assert "Sheet:" in result
        # Only first 20 rows returned (header + 19 data rows within limit)
        assert "Row 1:" in result
        assert "Row 20:" in result
        assert "Row 22:" not in result
        assert "Name" in result
        assert "user1" in result

    @pytest.mark.asyncio
    async def test_read_single_cell(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read a specific cell by coordinate."""
        monkeypatch.chdir(tmp_path)
        _create_basic_xlsx(
            tmp_path / "data.xlsx", ["Header1", "Header2"], [["val_a", 42]]
        )

        result = await tool.execute(
            path="data.xlsx", action="read", cells="B2"
        )
        assert "B2" in result
        assert "42" in result

    @pytest.mark.asyncio
    async def test_read_range(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read a range of cells like A1:C3."""
        monkeypatch.chdir(tmp_path)
        _create_basic_xlsx(
            tmp_path / "data.xlsx",
            ["H1", "H2", "H3"],
            [["a", "b", "c"], ["d", "e", "f"]],
        )

        result = await tool.execute(
            path="data.xlsx", action="read", cells="A1:C3"
        )
        assert "Range: A1:C3" in result
        assert "A1" in result
        assert "C3" in result
        assert "'H1'" in result

    @pytest.mark.asyncio
    async def test_read_comma_separated(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read comma-separated individual cells."""
        monkeypatch.chdir(tmp_path)
        _create_basic_xlsx(
            tmp_path / "data.xlsx",
            ["Col1", "Col2", "Col3"],
            [["v1", "v2", "v3"]],
        )

        result = await tool.execute(
            path="data.xlsx", action="read", cells="A1,B2,C3"
        )
        assert "A1" in result
        assert "B2" in result
        assert "C3" in result
        assert "'Col1'" in result
        assert "'v2'" in result

    @pytest.mark.asyncio
    async def test_read_specific_sheet(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read from a named sheet."""
        monkeypatch.chdir(tmp_path)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "DefaultSheet"
        ws2 = wb.create_sheet("DataSheet")
        ws2["A1"] = "OnDataSheet"
        ws2["B2"] = 99
        wb.save(str(tmp_path / "multi.xlsx"))

        result = await tool.execute(
            path="multi.xlsx", action="read", sheet_name="DataSheet", cells="A1"
        )
        assert "DataSheet" in result
        assert "'OnDataSheet'" in result

    @pytest.mark.asyncio
    async def test_read_show_formulas(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read with show_formulas=True shows formula strings."""
        monkeypatch.chdir(tmp_path)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = 10
        ws["B1"] = 20
        ws["C1"] = "=A1+B1"
        wb.save(str(tmp_path / "formula.xlsx"))

        # Default read (data_only=True) — formula cell shows computed or None
        result_values = await tool.execute(
            path="formula.xlsx", action="read", cells="C1"
        )
        # With data_only, openpyxl returns None for formula cells (no cached value)
        assert "C1" in result_values

        # show_formulas=True — formula string shown
        result_formulas = await tool.execute(
            path="formula.xlsx", action="read", cells="C1", show_formulas=True
        )
        assert "C1" in result_formulas
        assert "=A1+B1" in result_formulas
        assert "(formula)" in result_formulas


class TestExcelWorkbookFill:
    @pytest.mark.asyncio
    async def test_fill_cells(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fill cells with JSON values and verify they are saved."""
        monkeypatch.chdir(tmp_path)
        _create_basic_xlsx(tmp_path / "fill.xlsx", ["H1", "H2"], [["old1", "old2"]])

        cells_json = json.dumps({"A2": "new_val", "B2": 100})
        result = await tool.execute(
            path="fill.xlsx", action="fill", cells=cells_json
        )
        assert "Filled 2 cells" in result
        assert "Saved" in result

        # Verify values persisted
        wb = openpyxl.load_workbook(str(tmp_path / "fill.xlsx"))
        ws = wb.active
        assert ws["A2"].value == "new_val"
        assert ws["B2"].value == 100
        wb.close()

    @pytest.mark.asyncio
    async def test_fill_preserves_formatting(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fill does not erase existing cell formatting."""
        monkeypatch.chdir(tmp_path)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "Colored"
        ws["A1"].fill = PatternFill(
            start_color="FF0000", end_color="FF0000", fill_type="solid"
        )
        ws["B1"] = "Other"
        wb.save(str(tmp_path / "fmt.xlsx"))

        result = await tool.execute(
            path="fmt.xlsx", action="fill", cells=json.dumps({"B1": "updated"})
        )
        assert "Filled 1 cells" in result

        # Verify A1's fill color is preserved
        wb2 = openpyxl.load_workbook(str(tmp_path / "fmt.xlsx"))
        ws2 = wb2.active
        assert ws2["A1"].fill.start_color.rgb == "00FF0000"
        assert ws2["B1"].value == "updated"
        wb2.close()

    @pytest.mark.asyncio
    async def test_fill_preserves_merged(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fill does not destroy merged cell ranges."""
        monkeypatch.chdir(tmp_path)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.merge_cells("A1:B1")
        ws["A1"] = "MergedHeader"
        ws["C1"] = "Plain"
        wb.save(str(tmp_path / "merged.xlsx"))

        result = await tool.execute(
            path="merged.xlsx", action="fill", cells=json.dumps({"C1": "filled"})
        )
        assert "Filled 1 cells" in result

        # Verify merged range is preserved
        wb2 = openpyxl.load_workbook(str(tmp_path / "merged.xlsx"))
        ws2 = wb2.active
        assert "A1:B1" in [str(m) for m in ws2.merged_cells.ranges]
        assert ws2["C1"].value == "filled"
        wb2.close()

    @pytest.mark.asyncio
    async def test_fill_invalid_json(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed JSON in cells parameter returns an error."""
        monkeypatch.chdir(tmp_path)
        _create_basic_xlsx(tmp_path / "bad.xlsx", ["A"], [[1]])

        result = await tool.execute(
            path="bad.xlsx", action="fill", cells="{not valid json}"
        )
        assert "Error" in result
        assert "Invalid JSON" in result


class TestExcelWorkbookErrors:
    @pytest.mark.asyncio
    async def test_path_traversal_blocked(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path traversal with ../ is blocked."""
        monkeypatch.chdir(tmp_path)
        result = await tool.execute(
            path="../../../etc/passwd", action="read"
        )
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_non_xlsx_error(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-.xlsx file extension returns an error."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data.csv").write_text("a,b\n1,2", encoding="utf-8")

        result = await tool.execute(path="data.csv", action="read")
        assert "Error" in result
        assert ".xlsx" in result

    @pytest.mark.asyncio
    async def test_unknown_action_error(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid action returns an error."""
        monkeypatch.chdir(tmp_path)
        _create_basic_xlsx(tmp_path / "data.xlsx", ["A"], [[1]])

        result = await tool.execute(path="data.xlsx", action="delete")
        assert "Error" in result
        assert "Unknown action" in result

    @pytest.mark.asyncio
    async def test_missing_params(self, tool: ExcelWorkbookTool) -> None:
        """Missing required params returns errors."""
        result_no_path = await tool.execute(action="read")
        assert "Error" in result_no_path
        assert "path" in result_no_path

        result_no_action = await tool.execute(path="file.xlsx")
        assert "Error" in result_no_action
        assert "action" in result_no_action

    @pytest.mark.asyncio
    async def test_invalid_sheet_name(
        self, tool: ExcelWorkbookTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading from a nonexistent sheet returns error string."""
        monkeypatch.chdir(tmp_path)
        _create_basic_xlsx(tmp_path / "data.xlsx", ["A"], [[1]])

        result = await tool.execute(
            path="data.xlsx", action="read", sheet_name="NoSuchSheet"
        )
        assert "Error" in result
        assert "NoSuchSheet" in result
        assert "not found" in result
