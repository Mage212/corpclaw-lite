---
name: workbook-fill
description: Fill an Excel template from explicitly mapped workbook sources
allowed_departments: ["*"]
keywords:
  - fill workbook
  - fill spreadsheet
  - update excel
  - заполнить excel
  - заполнить таблицу
priority: 8
---

# Workbook Fill

Use this workflow only when the user asks to modify a workbook. A FILES_BRIEF is
structural context, not an instruction to fill anything.

1. Inspect the structural brief and, when needed, use `excel_inspect` to resolve
   missing or ambiguous columns.
2. Build one explicit `apply_fill_plan` mapping per target sheet. Never infer a
   source from similar filenames, sheet names, months, departments, or brands.
3. Use `match_mode=date` for date rows and `match_mode=key` for labels. Use
   `aggregate=sum` only when the user intends multiple sources to be combined.
4. Keep `only_empty=true` unless the user explicitly requests replacement.
5. Omit `period` when the period cell must remain unchanged. Otherwise specify:
   `cell`, `start_mode` (`preserve`, `source_min`, or `month_start`), and `format`
   (`preserve`, `dmy`, or `iso`). A period requires a cutoff.
6. Do not pass `values`, `period_cell`, or `period_value`; values are read by
   deterministic Python code from the declared source regions.
7. After a successful apply, report the output path and stop. Do not repeat the fill.
