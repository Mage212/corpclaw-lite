---
id: excel_normalizer
description: Normalize Excel files — fix INN, dates, numbers, remove invisible chars
version: "1.0.0"
allowed_for:
  - marketing
  - finance
  - hr
  - analytics
  - admin
  - default
keywords:
  - excel
  - xlsx
  - таблиц
  - нормализ
  - данны
  - столбец
  - колонк
  - заголов
  - формат
  - normalize
  - clean
  - spreadsheet
  - инн
  - дата
scope:
  - document-agent
---

# Excel Normalizer Skill

## Context

Use this skill when users need to normalize or clean Excel (.xlsx) files to prevent
data corruption when files are processed by external applications.

The tool fixes common Excel problems: INN displayed in scientific notation,
dates in datetime format instead of DD.MM.YYYY, floating-point rounding errors,
invisible Unicode characters, and missing leading zeros.

## What the tool does

1. **Removes invisible characters** — zero-width spaces, BOM, NBSP, soft hyphens, etc.
2. **Strict cell type enforcement:**
   - **INN columns**: converted to text, scientific notation eliminated, leading
     zeros restored. Header pattern examples: "инн".
   - **Date columns**: datetime → DD.MM.YYYY text, serial dates → DD.MM.YYYY
     text. Header pattern examples: "дата".
   - **Numeric columns**: floats rounded to 2 decimal places. Header pattern examples: "инвентарь", "сумма".
   - **Text columns**: floats like 123.0 converted to "123"
3. **Creates new formatted workbook** — Calibri 11pt bold headers, thin borders,
   auto-fitted column widths, freeze panes, proper cell number formats
4. **Removes completely empty rows**
5. **Preserves original headers** — no renaming, no snake_case

## When to use

- User says Excel shows exponents like 1.23E+10
- INN displays incorrectly (leading zeros lost)
- Numbers have extra digits (19.247999999)
- Dates are in datetime format instead of DD.MM.YYYY
- File needs to be standardized before uploading to another system

## Instructions

1. Use `normalize_excel` tool with `path` parameter.
2. Optionally specify `output_path` for custom output location.
3. Report the result: what was fixed, how many rows processed.

## Example

```
User: "Нормализуй файл report.xlsx"
Tool call: normalize_excel(path="report.xlsx")
```
