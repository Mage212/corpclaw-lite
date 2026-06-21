"""Deterministic PNG fixtures for vision eval scenarios (B-060).

Generates small, reproducible images so vision scenarios (read_image) can test
the VLM without shipping binary blobs in the repo. Output is fully determined
by the ``generator_id`` — no randomness, fixed layout, high contrast — so the
expected answer is stable across runs.

Supported generator ids:

- ``bar_chart_42`` — a single bar labelled "Value" with height 42, large number
  annotation. Tests numeric extraction from a chart.
- ``table_2x2`` — a 2×2 table (Sales 1500 / Costs 800). Tests cell extraction.
"""

# pyright: reportUnknownMemberType=warning

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["SUPPORTED_GENERATORS", "generate_image", "is_supported"]

# Keep in sync with the generators implemented below.
SUPPORTED_GENERATORS = frozenset({"bar_chart_42", "table_2x2"})


def is_supported(generator_id: str) -> bool:
    return generator_id in SUPPORTED_GENERATORS


def generate_image(generator_id: str, dest_path: Path | str) -> Path:
    """Generate a deterministic PNG for ``generator_id`` at ``dest_path``.

    Raises:
        ValueError: If ``generator_id`` is not in :data:`SUPPORTED_GENERATORS`.
    """
    if generator_id not in SUPPORTED_GENERATORS:
        raise ValueError(
            f"Unknown vision fixture generator '{generator_id}'. "
            f"Supported: {sorted(SUPPORTED_GENERATORS)}"
        )
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Agg backend = headless, no display required.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if generator_id == "bar_chart_42":
        _render_bar_chart_42(plt, dest)
    elif generator_id == "table_2x2":
        _render_table_2x2(plt, dest)
    plt.close("all")
    return dest


def _render_bar_chart_42(plt: Any, dest: Path) -> None:
    fig, ax = plt.subplots(figsize=(4, 3))
    ax.bar(["Value"], [42], color="#2E86AB")
    ax.set_ylim(0, 60)
    ax.set_ylabel("Value")
    ax.set_title("Q1 Result")
    # Large annotation so the number is unambiguous to the VLM.
    ax.text(0, 44, "42", ha="center", va="bottom", fontsize=24, fontweight="bold")
    fig.tight_layout()
    fig.savefig(str(dest), dpi=100)


def _render_table_2x2(plt: Any, dest: Path) -> None:
    fig, ax = plt.subplots(figsize=(4, 2))
    ax.axis("off")
    cell_text = [["1500"], ["800"]]
    row_labels = ["Sales", "Costs"]
    col_labels = ["Amount"]
    table = ax.table(
        cellText=cell_text,
        rowLabels=row_labels,
        colLabels=col_labels,
        cellLoc="center",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(16)
    table.scale(1.2, 1.8)
    fig.tight_layout()
    fig.savefig(str(dest), dpi=100)
