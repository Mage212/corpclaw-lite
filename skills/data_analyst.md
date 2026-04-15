---
id: data_analyst
description: "Analyze data files, generate charts, run SQL queries, and convert formats"
version: "1.0.0"
allowed_for:
  - analytics
  - finance
  - marketing
  - admin
  - development
  - engineering
keywords:
  - analyze
  - анализ
  - data
  - данны
  - chart
  - график
  - диаграмм
  - sql
  - query
  - запрос
  - стат
  - stat
  - report
  - отчет
  - отчёт
  - convert
  - конверт
  - table
  - таблиц
  - csv
  - xlsx
  - json
  - pdf
  - excel
  - визуал
  - visual
  - aggregat
  - filter
  - фильтр
  - group
  - группир
  - average
  - средн
  - sum
  - сумма
  - count
  - количе
---

# Data Analyst Skill

## Context

Use this skill when the user asks to analyze data, create charts, run queries
on tabular data, or convert between data formats. The user may have CSV, XLSX,
JSON, or PDF files in their workspace.

## Instructions

1. Identify what data source the user is working with. Use `list_files` to
   find available data files if the user doesn't specify one.
2. If the user asks about data content or structure, use `table_query` with
   a simple SQL query like `SELECT * FROM data LIMIT 10` to preview.
3. For analysis questions, construct appropriate SQL queries. Common patterns:
   - Aggregations: `SELECT column, COUNT(*), AVG(numeric_col) FROM data GROUP BY column`
   - Filtering: `SELECT * FROM data WHERE column = 'value'`
   - Sorting: `SELECT * FROM data ORDER BY column DESC LIMIT 20`
   - Top-N: `SELECT column, SUM(amount) as total FROM data GROUP BY column ORDER BY total DESC LIMIT 10`
4. If the user wants a chart, use `chart_generate`. Choose the appropriate
   chart type based on the data and question:
   - Comparisons between categories → bar chart
   - Trends over time → line chart
   - Proportions / parts of whole → pie chart
   - Relationships between variables → scatter chart
   - Distributions → histogram
5. If format conversion is needed (e.g., CSV to XLSX, JSON to CSV), use
   `convert_format`.
6. If the user has a PDF document with data, use `pdf_reader` to extract text
   first, then analyze the extracted content.
7. Always explain your findings in clear language. Include relevant numbers
   and percentages.
8. If the user asks for a file, use `send_file` to deliver results.
9. When saving analysis results, prefer CSV or XLSX format for tabular data,
   and PNG for charts.

## Examples

**Input:** "Analyze sales.csv and show me top 10 products by revenue"
**Output:** Use `table_query` with `SELECT product, SUM(revenue) as total_revenue FROM data GROUP BY product ORDER BY total_revenue DESC LIMIT 10`, then present results and optionally generate a bar chart with `chart_generate`.

**Input:** "Сделай диаграмму по регионам из файла regions.xlsx"
**Output:** Preview data with `table_query` (`SELECT * FROM data LIMIT 5`), then use `chart_generate` with `chart_type="bar"` to create a bar chart by region.

**Input:** "Convert data.json to Excel format"
**Output:** Use `convert_format` with `input_path="data.json"` and `output_format="xlsx"`.
