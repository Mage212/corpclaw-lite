"""Unit tests for B-095 pin budget (25% hard cap)."""

from __future__ import annotations

from corpclaw_lite.agent.pin_budget import (
    DEFAULT_PIN_CONTEXT_RATIO,
    evaluate_pin_budget,
    pin_budget_limit,
)


def test_pin_budget_limit_25_percent() -> None:
    assert pin_budget_limit(10_000) == 2500
    assert pin_budget_limit(64000) == 16000
    assert pin_budget_limit(1) == 1


def test_evaluate_pin_budget_allow() -> None:
    r = evaluate_pin_budget(
        current_pinned_tokens=1000,
        new_tokens=500,
        context_limit=10_000,
    )
    assert r.allowed is True
    assert r.pin_budget == 2500
    assert r.projected_tokens == 1500
    assert r.pin_ratio == DEFAULT_PIN_CONTEXT_RATIO


def test_evaluate_pin_budget_block_over_25() -> None:
    r = evaluate_pin_budget(
        current_pinned_tokens=2000,
        new_tokens=600,
        context_limit=10_000,
    )
    assert r.allowed is False
    assert r.projected_tokens == 2600
    assert r.pin_budget == 2500
    assert "25%" in r.reason


def test_evaluate_pin_budget_exact_cap() -> None:
    r = evaluate_pin_budget(
        current_pinned_tokens=2000,
        new_tokens=500,
        context_limit=10_000,
    )
    assert r.allowed is True
    assert r.projected_tokens == 2500
