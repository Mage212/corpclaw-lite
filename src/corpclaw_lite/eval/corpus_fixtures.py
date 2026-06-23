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

SUPPORTED_GENERATORS = frozenset({"messy_headers"})


def is_supported(generator_id: str) -> bool:
    return generator_id in SUPPORTED_GENERATORS


def generate_workbook(generator_id: str, dest: Path) -> None:
    """Generate a deterministic ``.xlsx`` fixture at ``dest``.

    Raises:
        ValueError: if ``generator_id`` is not a supported generator.
    """
    if generator_id == "messy_headers":
        _generate_messy_headers(dest)
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
