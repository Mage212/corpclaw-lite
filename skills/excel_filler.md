---
id: excel_filler
description: "Fill Excel templates with data — read-before-write workflow for accurate cell placement"
version: "1.0.0"
allowed_for:
  - marketing
  - finance
  - hr
  - analytics
  - admin
  - default
  - development
  - engineering
keywords:
  - заполни
  - fill
  - шаблон
  - template
  - перенес
  - вставь
  - данные
  - data
  - отчет
  - отчёт
  - report
  - excel
  - xlsx
  - заполн
  - встав
scope:
  - data-agent
  - document-agent
---

# Excel Template Filler

## Context

Use this skill when the task requires filling an Excel template with data from another file.
Typical requests: "fill the report template with data from file X", "transfer these values into the Excel form".

## CRITICAL RULES

### Rule 1: NEVER trust row numbers from the task description

The task may say "fill rows 13-17" or "April 13-17 data". These are **dates or labels**, NOT Excel row
numbers. The actual data might be in rows 28-32. You MUST discover the correct rows by reading the file.

### Rule 2: ALWAYS read before writing — two-phase read

**Phase 1 — Read the filled part** of the template to understand the pattern.
Use `excel_workbook` with `action=read` covering existing data rows.
Learn which columns hold which values, what format they use, how rows map to dates.

**Phase 2 — Read the target part** with extra margin (at least 5 rows above and below).
If you expect data around row 25, read rows 20-35.
Look for date labels, headers, empty rows — structural markers that reveal where data goes.

Only after both reads proceed to fill.

### Rule 3: Verify after filling

After filling, read back the filled cells to confirm values were placed correctly.

## Step-by-step Workflow

1. **Read source data** — use `table_query` (`SELECT * FROM data LIMIT 20`) or `excel_workbook action=read`.
2. **Read template — filled zone** — `excel_workbook action=read` on the range that already has data.
   Understand the column mapping (G=Показы, J=Охват, etc.) and how rows correspond to dates.
3. **Read template — target zone** — read the area that needs filling **with generous margin**.
   Find the exact rows by matching date labels in the template, not by assuming row numbers.
4. **Build fill map** — create `{"G28": 39015, "G29": 41200, ...}` based on discovered positions.
5. **Fill** — `excel_workbook action=fill` with the cell map.
6. **Verify** — read back the filled cells and compare with source data.

## What NOT to do

- DO NOT assume "13-17 апреля" means rows 13-17 — these are dates, find their actual row positions
- DO NOT skip the read steps even if the task provides row numbers
- DO NOT worry about merged cells — the tool skips them automatically
- DO NOT fill without verifying the result
