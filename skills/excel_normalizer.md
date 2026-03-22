---
id: excel_normalizer
description: Normalize and clean Excel/CSV files — fix headers, types, duplicates
version: "1.0.0"
allowed_for:
  - marketing
  - finance
  - hr
  - analytics
  - default
---

# Excel Normalizer Skill

## Context

Use this skill when users need to clean or normalize tabular data from Excel (.xlsx) or CSV files.
Common tasks: fixing column names, removing duplicates, standardizing date formats,
filling missing values, converting number formats.

## Instructions

1. Use `read_file` to read the file if it's CSV, or ask the user to export to CSV first.
2. Identify the issues: inconsistent headers, mixed types, blank rows, duplicate rows.
3. Describe what transformations you will apply before making changes.
4. Apply transformations and write the cleaned file using `write_file`.
5. Report: how many rows processed, how many duplicates removed, what was changed.

## Rules

- Never delete data without informing the user first.
- Preserve original column order unless asked to change it.
- For date normalization, use ISO 8601 format (YYYY-MM-DD) by default.
- If a column has >50% missing values, flag it to the user instead of silently dropping.

## Examples

**Input:** "Нормализуй этот CSV: у него разные форматы дат и дубликаты в колонке email."
**Output:** (read file → report issues → write cleaned file → summary of changes)
