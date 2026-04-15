"""Tests for ConvertFormatTool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpclaw_lite.extensions.tools.builtin.convert_format import ConvertFormatTool


@pytest.fixture
def tool() -> ConvertFormatTool:
    return ConvertFormatTool()


def _create_csv(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _create_xlsx(path: Path, headers: list[str], rows: list[list[object]]) -> Path:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for col, h in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=h)
    for row_idx, row in enumerate(rows, 2):
        for col, val in enumerate(row, 1):
            ws.cell(row=row_idx, column=col, value=val)
    wb.save(str(path))
    return path


def _create_json(path: Path, data: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _create_markdown(path: Path, headers: list[str], rows: list[list[str]]) -> Path:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestConvertFormatTool:
    @pytest.mark.asyncio
    async def test_csv_to_json(
        self, tool: ConvertFormatTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "name,age\nAlice,30\nBob,25\n")

        result = await tool.execute(input_path="data.csv", output_format="json")
        assert "Converted" in result
        assert (tmp_path / "data.json").exists()
        data = json.loads((tmp_path / "data.json").read_text())
        assert len(data) == 2
        assert data[0]["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_json_to_csv(
        self, tool: ConvertFormatTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_json(tmp_path / "data.json", [{"x": 1, "y": 2}, {"x": 3, "y": 4}])

        result = await tool.execute(input_path="data.json", output_format="csv")
        assert "Converted" in result
        assert (tmp_path / "data.csv").exists()
        content = (tmp_path / "data.csv").read_text()
        assert "x" in content

    @pytest.mark.asyncio
    async def test_xlsx_to_csv(
        self, tool: ConvertFormatTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_xlsx(tmp_path / "data.xlsx", ["city", "pop"], [["Moscow", "12M"], ["SPb", "5M"]])

        result = await tool.execute(input_path="data.xlsx", output_format="csv")
        assert "Converted" in result
        content = (tmp_path / "data.csv").read_text()
        assert "Moscow" in content

    @pytest.mark.asyncio
    async def test_csv_to_markdown(
        self, tool: ConvertFormatTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "name,value\nAlice,100\nBob,200\n")

        result = await tool.execute(input_path="data.csv", output_format="markdown")
        assert "Converted" in result
        content = (tmp_path / "data.md").read_text()
        assert "| name" in content
        assert "Alice" in content

    @pytest.mark.asyncio
    async def test_markdown_to_csv(
        self, tool: ConvertFormatTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_markdown(tmp_path / "table.md", ["col1", "col2"], [["a", "b"], ["c", "d"]])

        result = await tool.execute(input_path="table.md", output_format="csv")
        assert "Converted" in result
        content = (tmp_path / "table.csv").read_text()
        assert "col1" in content
        assert "a" in content

    @pytest.mark.asyncio
    async def test_csv_to_xlsx(
        self, tool: ConvertFormatTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "x,y\n1,2\n3,4\n")

        result = await tool.execute(input_path="data.csv", output_format="xlsx")
        assert "Converted" in result
        assert (tmp_path / "data.xlsx").exists()

    @pytest.mark.asyncio
    async def test_custom_output_path(
        self, tool: ConvertFormatTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "a,b\n1,2\n")

        result = await tool.execute(
            input_path="data.csv", output_format="json", output_path="custom.json"
        )
        assert "custom.json" in result
        assert (tmp_path / "custom.json").exists()

    @pytest.mark.asyncio
    async def test_file_not_found(self, tool: ConvertFormatTool) -> None:
        result = await tool.execute(input_path="nonexistent.csv", output_format="json")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_unsupported_input(
        self, tool: ConvertFormatTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data.txt").write_text("hello", encoding="utf-8")

        result = await tool.execute(input_path="data.txt", output_format="json")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_missing_params(self, tool: ConvertFormatTool) -> None:
        result = await tool.execute()
        assert "Error" in result
