"""Tests for ChartGenerateTool."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.extensions.tools.builtin.chart_generate import ChartGenerateTool


@pytest.fixture
def tool() -> ChartGenerateTool:
    return ChartGenerateTool()


def _create_csv(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


_PNG_MAGIC = b"\x89PNG"


class TestChartGenerateTool:
    @pytest.mark.asyncio
    async def test_bar_chart(
        self, tool: ChartGenerateTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "city,population\nMoscow,12\nSPb,5\nKazan,1.5\n")

        result = await tool.execute(
            data_path="data.csv",
            chart_type="bar",
            x_column="city",
            y_column="population",
            title="Population",
        )
        assert "Chart saved" in result
        output = tmp_path / "chart.png"
        assert output.exists()
        assert output.read_bytes()[:4] == _PNG_MAGIC

    @pytest.mark.asyncio
    async def test_line_chart(
        self, tool: ChartGenerateTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "month,sales\nJan,100\nFeb,150\nMar,120\n")

        result = await tool.execute(data_path="data.csv", chart_type="line", x_column="month", y_column="sales")
        assert "Chart saved" in result

    @pytest.mark.asyncio
    async def test_pie_chart(
        self, tool: ChartGenerateTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "category,value\nA,40\nB,30\nC,30\n")

        result = await tool.execute(data_path="data.csv", chart_type="pie", x_column="category", y_column="value")
        assert "Chart saved" in result

    @pytest.mark.asyncio
    async def test_scatter_chart(
        self, tool: ChartGenerateTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "x,y\n1,2\n3,4\n5,1\n7,8\n")

        result = await tool.execute(data_path="data.csv", chart_type="scatter")
        assert "Chart saved" in result

    @pytest.mark.asyncio
    async def test_histogram(
        self, tool: ChartGenerateTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "score\n85\n92\n78\n95\n88\n91\n76\n89\n")

        result = await tool.execute(data_path="data.csv", chart_type="histogram", y_column="score")
        assert "Chart saved" in result

    @pytest.mark.asyncio
    async def test_custom_output_path(
        self, tool: ChartGenerateTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "x,y\n1,2\n3,4\n")

        result = await tool.execute(
            data_path="data.csv",
            chart_type="bar",
            output_path="my_chart.png",
        )
        assert "my_chart.png" in result
        assert (tmp_path / "my_chart.png").exists()

    @pytest.mark.asyncio
    async def test_file_not_found(self, tool: ChartGenerateTool) -> None:
        result = await tool.execute(data_path="nonexistent.csv", chart_type="bar")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_missing_chart_type(
        self, tool: ChartGenerateTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "x,y\n1,2\n")

        result = await tool.execute(data_path="data.csv")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_auto_columns(
        self, tool: ChartGenerateTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that chart works without specifying x/y columns."""
        monkeypatch.chdir(tmp_path)
        _create_csv(tmp_path / "data.csv", "name,value\nA,10\nB,20\nC,30\n")

        result = await tool.execute(data_path="data.csv", chart_type="bar")
        assert "Chart saved" in result
