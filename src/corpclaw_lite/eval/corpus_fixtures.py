"""Deterministic xlsx workbook fixtures for eval scenarios (B-060).

Mirrors :mod:`corpclaw_lite.eval.vision_fixtures` but for binary spreadsheets.
Generates reproducible ``.xlsx`` files so office scenarios (normalize_excel,
excel_workbook) can run without shipping binary blobs or relying on an external
``--corpus-dir``. Output is fully determined by ``generator_id`` — fixed headers,
fixed rows — so the expected answer is stable across runs.

Supported generator ids:

- ``messy_headers`` — a sheet with intentionally messy column headers (extra
  leading/trailing spaces, mixed case: ``"  Name  "``, ``"DEPARTMENT"``,
  ``"salary  "``, ``"Hire Date"``). Tests that ``normalize_excel`` strips
  whitespace and lowercases. Two data rows so the structure is non-trivial.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["SUPPORTED_GENERATORS", "generate_workbook", "is_supported"]

SUPPORTED_GENERATORS = frozenset(
    {"messy_headers", "fill_date_source", "fill_key_source", "fill_target"}
)


def is_supported(generator_id: str) -> bool:
    return generator_id in SUPPORTED_GENERATORS


def generate_workbook(generator_id: str, dest: Path) -> None:
    """Generate a deterministic ``.xlsx`` fixture at ``dest``.

    Raises:
        ValueError: if ``generator_id`` is not a supported generator.
    """
    if generator_id == "messy_headers":
        _generate_messy_headers(dest)
    elif generator_id == "fill_date_source":
        _generate_fill_date_source(dest)
    elif generator_id == "fill_key_source":
        _generate_fill_key_source(dest)
    elif generator_id == "fill_target":
        _generate_fill_target(dest)
    else:
        raise ValueError(
            f"Unknown workbook generator: {generator_id!r}. "
            f"Supported: {sorted(SUPPORTED_GENERATORS)}"
        )


def _generate_messy_headers(dest: Path) -> None:
    """Create an xlsx with intentionally messy headers.

    Headers: ``"  Name  "``, ``"DEPARTMENT"``, ``"salary  "``, ``"Hire Date"``
    — extra spaces and mixed case that ``normalize_excel`` should clean to
    ``name``, ``department``, ``salary``, ``hire date``. Two data rows keep the
    structure realistic for an inspection/normalise round-trip.
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None  # Workbook() always creates one sheet
    ws.title = "Sheet1"
    ws.append(["  Name  ", "DEPARTMENT", "salary  ", "Hire Date"])
    ws.append(["Alice", "Sales", 50000, "2023-01-15"])
    ws.append(["Bob", "  Marketing  ", 55000, "2023-03-20"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)


def _generate_fill_date_source(dest: Path) -> None:
    from datetime import date

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "DailyData"
    ws.append(["Date", "Value"])
    ws.append([date(2030, 3, 8), 100])
    ws.append([date(2030, 3, 9), 7])
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)


def _generate_fill_key_source(dest: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Labels"
    ws.append(["Name", "Value"])
    ws.append(["Item A", 11])
    ws.append(["Item B", 13])
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)


def _generate_fill_target(dest: Path) -> None:
    from datetime import date

    from openpyxl import Workbook

    wb = Workbook()
    daily = wb.active
    assert daily is not None
    daily.title = "Daily"
    daily["C1"] = "Reporting period"
    daily["D1"] = "2030-03-01 - 2030-03-31"
    daily.append(["Date", "Value"])
    daily.append([date(2030, 3, 8), None])
    daily.append([date(2030, 3, 9), None])
    keyed = wb.create_sheet("ByLabel")
    keyed.append(["Name", "Value"])
    keyed.append(["Item A", None])
    keyed.append(["Item B", None])
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)
