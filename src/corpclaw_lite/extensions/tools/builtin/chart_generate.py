# pyright: reportUnknownMemberType=warning
"""chart_generate — generate charts from tabular data files.

Uses matplotlib with Agg backend (headless) to render charts as PNG.
Data is loaded via DuckDB (reuses table_query patterns).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam  # noqa: E402
from corpclaw_lite.extensions.tools.builtin.files import resolve_and_validate_path  # noqa: E402
from corpclaw_lite.extensions.tools.builtin.table_query import (  # noqa: E402
    detect_csv_encoding,
    init_duckdb_with_xlsx,
    load_xlsx_as_dicts,
    reencode_csv_to_utf8,
)
from corpclaw_lite.utils.async_helpers import run_in_thread  # noqa: E402

__all__ = ["ChartGenerateTool"]

_MAX_DATA_POINTS = 10_000


def _load_columns(path: Path) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Load data file into columns and rows via DuckDB."""
    import duckdb

    conn = duckdb.connect(":memory:")
    try:
        ext = path.suffix.lower()
        # Escape single quotes in path to prevent SQL injection via file names.
        p = str(path).replace("'", "''")

        if ext == ".csv":
            enc = detect_csv_encoding(path)
            if enc in ("utf-8", "utf-8-sig"):
                conn.execute(
                    f"CREATE TABLE data AS SELECT * FROM read_csv_auto('{p}', encoding='utf-8')"
                )
            else:
                utf8_path = reencode_csv_to_utf8(path, enc)
                try:
                    up = str(utf8_path).replace("'", "''")
                    conn.execute(f"CREATE TABLE data AS SELECT * FROM read_csv_auto('{up}')")
                finally:
                    utf8_path.unlink(missing_ok=True)
        elif ext in (".json", ".jsonl", ".ndjson"):
            conn.execute(f"CREATE TABLE data AS SELECT * FROM read_json_auto('{p}')")
        elif ext == ".xlsx":
            rows = load_xlsx_as_dicts(path)
            if not rows:
                return [], []
            init_duckdb_with_xlsx(path, rows, conn)
        elif ext == ".parquet":
            conn.execute(f"CREATE TABLE data AS SELECT * FROM read_parquet('{p}')")
        else:
            raise ValueError(f"Unsupported file format: {ext}")

        result = conn.execute("SELECT * FROM data LIMIT ?", [_MAX_DATA_POINTS])
        columns = [desc[0] for desc in result.description] if result.description else []
        rows = result.fetchall()
        return columns, rows
    finally:
        conn.close()


def _generate_chart(
    data_path: Path,
    chart_type: str,
    x_column: str | None,
    y_column: str | None,
    title: str | None,
    output_path: Path | None,
) -> str:
    columns, rows = _load_columns(data_path)

    if not columns or not rows:
        return "Error: No data found in file."

    # Resolve output path.
    if output_path is None:
        output_path = data_path.parent / "chart.png"

    fig, ax = plt.subplots(figsize=(10, 6))

    try:
        if chart_type == "bar":
            x_col = x_column or columns[0]
            y_col = y_column or (columns[1] if len(columns) > 1 else columns[0])
            x_idx = columns.index(x_col) if x_col in columns else 0
            y_idx = columns.index(y_col) if y_col in columns else (1 if len(columns) > 1 else 0)

            labels = [str(row[x_idx]) for row in rows]
            values = [float(row[y_idx]) if row[y_idx] is not None else 0 for row in rows]
            ax.bar(labels, values)
            ax.set_xlabel(x_col)
            ax.set_ylabel(y_col)
            plt.xticks(rotation=45, ha="right")

        elif chart_type == "line":
            x_col = x_column or columns[0]
            y_col = y_column or (columns[1] if len(columns) > 1 else columns[0])
            x_idx = columns.index(x_col) if x_col in columns else 0
            y_idx = columns.index(y_col) if y_col in columns else (1 if len(columns) > 1 else 0)

            x_vals = [str(row[x_idx]) for row in rows]
            y_vals = [float(row[y_idx]) if row[y_idx] is not None else 0 for row in rows]
            ax.plot(x_vals, y_vals, marker="o", markersize=3)
            ax.set_xlabel(x_col)
            ax.set_ylabel(y_col)
            plt.xticks(rotation=45, ha="right")

        elif chart_type == "pie":
            x_col = x_column or columns[0]
            y_col = y_column or (columns[1] if len(columns) > 1 else columns[0])
            x_idx = columns.index(x_col) if x_col in columns else 0
            y_idx = columns.index(y_col) if y_col in columns else (1 if len(columns) > 1 else 0)

            labels = [str(row[x_idx]) for row in rows]
            values = [float(row[y_idx]) if row[y_idx] is not None else 0 for row in rows]
            ax.pie(values, labels=labels, autopct="%1.1f%%")

        elif chart_type == "scatter":
            x_col = x_column or columns[0]
            y_col = y_column or (columns[1] if len(columns) > 1 else columns[0])
            x_idx = columns.index(x_col) if x_col in columns else 0
            y_idx = columns.index(y_col) if y_col in columns else (1 if len(columns) > 1 else 0)

            x_vals = [float(row[x_idx]) if row[x_idx] is not None else 0 for row in rows]
            y_vals = [float(row[y_idx]) if row[y_idx] is not None else 0 for row in rows]
            ax.scatter(x_vals, y_vals, s=10, alpha=0.6)
            ax.set_xlabel(x_col)
            ax.set_ylabel(y_col)

        elif chart_type == "histogram":
            col = y_column or x_column or columns[0]
            idx = columns.index(col) if col in columns else 0
            values = [float(row[idx]) if row[idx] is not None else 0 for row in rows]
            ax.hist(values, bins=min(30, len(values)))
            ax.set_xlabel(col)
            ax.set_ylabel("Frequency")

        else:
            return f"Error: Unknown chart type '{chart_type}'."

        if title:
            ax.set_title(title)

        fig.tight_layout()
        fig.savefig(str(output_path), dpi=100)
        return f"Chart saved to {output_path} ({chart_type}, {len(rows)} data points)"

    except Exception as e:
        return f"Error generating chart: {e}"
    finally:
        plt.close(fig)


class ChartGenerateTool(Tool):
    """Generate charts and graphs from tabular data files."""

    name = "chart_generate"
    description = (
        "Generate charts from tabular data files (CSV, XLSX, JSON). "
        "Supports bar, line, pie, scatter, and histogram chart types."
    )
    params = [
        ToolParam(
            name="data_path",
            type="string",
            description="Path to data file (CSV, XLSX, JSON)",
        ),
        ToolParam(
            name="chart_type",
            type="string",
            description="Type of chart to generate",
            enum=["bar", "line", "pie", "scatter", "histogram"],
        ),
        ToolParam(
            name="x_column",
            type="string",
            description="Column for X axis (or labels for pie chart)",
            required=False,
        ),
        ToolParam(
            name="y_column",
            type="string",
            description="Column for Y axis (or values for pie chart)",
            required=False,
        ),
        ToolParam(
            name="title",
            type="string",
            description="Chart title",
            required=False,
        ),
        ToolParam(
            name="output_path",
            type="string",
            description="Output image path (default: chart.png)",
            required=False,
        ),
    ]
    risk_level = RiskLevel.MEDIUM

    async def execute(self, **kwargs: Any) -> str:
        data_path_str = kwargs.get("data_path", "")
        chart_type = kwargs.get("chart_type", "")
        x_column = kwargs.get("x_column")
        y_column = kwargs.get("y_column")
        title = kwargs.get("title")
        output_str = kwargs.get("output_path")

        if not data_path_str:
            return "Error: 'data_path' is required."
        if not chart_type:
            return "Error: 'chart_type' is required."

        try:
            resolved = resolve_and_validate_path(data_path_str)
        except PermissionError as e:
            return f"Error: {e}"

        if not resolved.is_file():
            return f"Error: File not found: {data_path_str}"

        output_path: Path | None = None
        if output_str:
            try:
                output_path = resolve_and_validate_path(output_str)
            except PermissionError as e:
                return f"Error: {e}"

        return await run_in_thread(
            _generate_chart, resolved, chart_type, x_column, y_column, title, output_path
        )
