"""Unit tests for B-093 budget-gate (pure + async helper)."""

from __future__ import annotations

import httpx
import pytest

from corpclaw_lite.agent.budget_gate import (
    BudgetDecision,
    estimate_content_budget,
    evaluate_budget,
)
from corpclaw_lite.llm.tokenizer_client import TokenizerClient


def test_allow_small_file_empty_baseline() -> None:
    result = evaluate_budget(baseline=0, file_cost=100, limit=1000)
    assert result.decision is BudgetDecision.ALLOW
    assert result.offer_chunked is False
    assert result.projected_tokens == 100
    assert result.ratio_after == 0.1
    assert "помещается" in result.reason.lower() or "помещается" in result.reason


def test_warn_at_0_90_limit() -> None:
    result = evaluate_budget(baseline=800, file_cost=100, limit=1000)
    assert result.projected_tokens == 900
    assert result.decision is BudgetDecision.WARN
    assert result.offer_chunked is False


def test_block_at_0_96_limit_disables_chunked() -> None:
    # file > 0.5 limit but BLOCK wins → no chunked offer
    result = evaluate_budget(baseline=400, file_cost=560, limit=1000)
    assert result.projected_tokens == 960
    assert result.decision is BudgetDecision.BLOCK
    assert result.offer_chunked is False
    assert "Не поместится" in result.reason


def test_allow_with_offer_chunked_when_file_huge() -> None:
    result = evaluate_budget(baseline=0, file_cost=600, limit=1000)
    assert result.decision is BudgetDecision.ALLOW
    assert result.offer_chunked is True
    assert "порц" in result.reason.lower()


def test_warn_with_offer_chunked() -> None:
    # projected 0.90, file > 0.5 limit
    result = evaluate_budget(baseline=300, file_cost=600, limit=1000)
    assert result.projected_tokens == 900
    assert result.decision is BudgetDecision.WARN
    assert result.offer_chunked is True


def test_limit_zero_coerced_to_one() -> None:
    result = evaluate_budget(baseline=0, file_cost=0, limit=0)
    assert result.limit_tokens == 1
    assert result.decision is BudgetDecision.ALLOW


def test_negative_baseline_clamped() -> None:
    result = evaluate_budget(baseline=-50, file_cost=10, limit=1000)
    assert result.baseline_tokens == 0
    assert result.projected_tokens == 10


def test_to_dict_keys() -> None:
    result = evaluate_budget(baseline=1, file_cost=2, limit=100)
    data = result.to_dict()
    assert data["decision"] == "allow"
    assert data["file_tokens"] == 2
    assert data["approximate"] is False
    assert data["source"] == "heuristic"


@pytest.mark.asyncio
async def test_estimate_content_budget_heuristic() -> None:
    client = TokenizerClient(mode="heuristic")
    content = "abcd" * 100  # 400 chars → 100 tokens heuristic
    result = await estimate_content_budget(content, baseline=0, limit=10_000, tokenizer=client)
    assert result.file_tokens == 100
    assert result.approximate is True
    assert result.source == "heuristic"
    assert result.decision is BudgetDecision.ALLOW


@pytest.mark.asyncio
async def test_estimate_content_budget_tokenize_mock() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tokens": list(range(42))})

    client = TokenizerClient(
        base_url="http://host:8080",
        mode="tokenize",
        transport=httpx.MockTransport(handler),
    )
    result = await estimate_content_budget("payload", baseline=0, limit=1000, tokenizer=client)
    assert result.file_tokens == 42
    assert result.approximate is False
    assert result.source == "tokenize"
    assert result.decision is BudgetDecision.ALLOW
