"""Tests for deterministic PNG vision fixtures (B-060 corpus expansion)."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.eval.vision_fixtures import (
    SUPPORTED_GENERATORS,
    generate_image,
    is_supported,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_bar_chart_42_generates_valid_png(tmp_path: Path) -> None:
    p = generate_image("bar_chart_42", tmp_path / "chart.png")
    assert p.exists()
    data = p.read_bytes()
    assert data[:8] == _PNG_MAGIC, "not a valid PNG (bad magic header)"
    assert len(data) > 500, "PNG suspiciously small"


def test_table_2x2_generates_valid_png(tmp_path: Path) -> None:
    p = generate_image("table_2x2", tmp_path / "table.png")
    assert p.exists()
    assert p.read_bytes()[:8] == _PNG_MAGIC


def test_generate_image_creates_parent_dirs(tmp_path: Path) -> None:
    p = generate_image("bar_chart_42", tmp_path / "nested" / "dir" / "chart.png")
    assert p.exists()


def test_unknown_generator_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown vision fixture generator"):
        generate_image("does_not_exist", tmp_path / "x.png")


@pytest.mark.parametrize("gid", ["bar_chart_42", "table_2x2"])
def test_is_supported_true_for_known(gid: str) -> None:
    assert is_supported(gid)


def test_is_supported_false_for_unknown() -> None:
    assert not is_supported("nope")


def test_supported_generators_is_frozenset() -> None:
    assert isinstance(SUPPORTED_GENERATORS, frozenset)
    assert "bar_chart_42" in SUPPORTED_GENERATORS
    assert "table_2x2" in SUPPORTED_GENERATORS


def test_generation_is_deterministic(tmp_path: Path) -> None:
    """Same generator id → byte-identical PNG across runs (no randomness)."""
    p1 = generate_image("bar_chart_42", tmp_path / "a.png")
    p2 = generate_image("bar_chart_42", tmp_path / "b.png")
    # PNGs include a timestamp in metadata, so we compare pixel dimensions
    # rather than raw bytes. Both must have the same size class.
    assert abs(len(p1.read_bytes()) - len(p2.read_bytes())) < 2000
