"""Unit tests for the core-version constraint parser (PR-4).

Covers satisfies_core_version semantics: empty constraint, bare exact match,
caret (0.x pins minor, 1.x+ pins major), and the unknown-core non-blocking
fallback.
"""

from __future__ import annotations

from corpclaw_lite.extensions.plugins.core_version import satisfies_core_version


def test_empty_constraint_always_satisfied() -> None:
    assert satisfies_core_version("", "0.1.11")
    assert satisfies_core_version("", "99.99.99")


def test_bare_constraint_exact_match() -> None:
    assert satisfies_core_version("0.1.11", "0.1.11")
    assert not satisfies_core_version("0.1.11", "0.1.12")
    assert not satisfies_core_version("0.1.11", "0.2.0")


def test_caret_pins_minor_for_zero_x() -> None:
    # ^0.1.11 — 0.1.x compatible, 0.2.0 not (minor bump is breaking pre-1.0).
    assert satisfies_core_version("^0.1.11", "0.1.11")
    assert satisfies_core_version("^0.1.11", "0.1.20")
    assert not satisfies_core_version("^0.1.11", "0.2.0")
    assert not satisfies_core_version("^0.1.11", "0.0.5")  # below the floor


def test_caret_pins_major_for_one_x_plus() -> None:
    assert satisfies_core_version("^1.2.3", "1.2.3")
    assert satisfies_core_version("^1.2.3", "1.9.9")
    assert not satisfies_core_version("^1.2.3", "2.0.0")
    assert not satisfies_core_version("^1.2.3", "1.2.2")  # below the floor


def test_unknown_core_does_not_block() -> None:
    # When the core version can't be determined (editable checkout), never block.
    assert satisfies_core_version("^0.1.11", "0.0.0+unknown")
    assert satisfies_core_version("^99.0.0", "0.0.0+unknown")


def test_parser_strips_prerelease_suffix() -> None:
    # Pre-release suffixes don't break parsing; the numeric part is compared.
    assert satisfies_core_version("^0.1.11", "0.1.11-rc1")


def test_partial_version_pads_to_zero() -> None:
    assert satisfies_core_version("1.2", "1.2.0")
    assert satisfies_core_version("^0.1", "0.1.5")
