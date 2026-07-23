"""Pre-flight context budget gate for add-to-context (B-093 / DC-013).

Pure threshold logic plus a thin async helper that uses
:class:`~corpclaw_lite.llm.tokenizer_client.TokenizerClient` for file cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from corpclaw_lite.llm.tokenizer_client import TokenSource

if TYPE_CHECKING:
    from corpclaw_lite.llm.tokenizer_client import TokenizerClient

__all__ = [
    "BudgetDecision",
    "BudgetGateResult",
    "DEFAULT_BLOCK_RATIO",
    "DEFAULT_CHUNKED_FILE_RATIO",
    "DEFAULT_WARN_RATIO",
    "estimate_content_budget",
    "evaluate_budget",
]

DEFAULT_WARN_RATIO = 0.85
DEFAULT_BLOCK_RATIO = 0.95
DEFAULT_CHUNKED_FILE_RATIO = 0.5

_REASON_ALLOW = "Файл помещается в контекст."
_REASON_ALLOW_CHUNKED = "Файл большой; лучше добавить порцию."
_REASON_WARN = "После добавления останется мало места в контексте."
_REASON_WARN_CHUNKED = "После добавления останется мало места; файл большой — лучше порция."
_REASON_BLOCK = "Не поместится в контекст (нужно освободить место: сжатие/удаление)."


class BudgetDecision(StrEnum):
    """Pre-flight decision for attaching content to the agent context."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class BudgetGateResult:
    """Outcome of baseline + file_cost vs context limit."""

    decision: BudgetDecision
    offer_chunked: bool
    baseline_tokens: int
    file_tokens: int
    projected_tokens: int
    limit_tokens: int
    ratio_after: float
    warn_ratio: float
    block_ratio: float
    approximate: bool
    source: TokenSource
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "offer_chunked": self.offer_chunked,
            "baseline_tokens": self.baseline_tokens,
            "file_tokens": self.file_tokens,
            "projected_tokens": self.projected_tokens,
            "limit_tokens": self.limit_tokens,
            "ratio_after": self.ratio_after,
            "warn_ratio": self.warn_ratio,
            "block_ratio": self.block_ratio,
            "approximate": self.approximate,
            "source": self.source,
            "reason": self.reason,
        }


def evaluate_budget(
    *,
    baseline: int,
    file_cost: int,
    limit: int,
    warn_ratio: float = DEFAULT_WARN_RATIO,
    block_ratio: float = DEFAULT_BLOCK_RATIO,
    chunked_file_ratio: float = DEFAULT_CHUNKED_FILE_RATIO,
    approximate: bool = False,
    source: TokenSource = "heuristic",
) -> BudgetGateResult:
    """Compare baseline + file_cost against context limit thresholds.

    Priority: BLOCK wins over WARN/ALLOW. ``offer_chunked`` is only True for
    ALLOW/WARN when the file alone exceeds ``chunked_file_ratio * limit``.
    """
    safe_limit = max(1, int(limit))
    safe_baseline = max(0, int(baseline))
    safe_file = max(0, int(file_cost))
    projected = safe_baseline + safe_file
    ratio_after = projected / safe_limit

    if projected > safe_limit * block_ratio:
        decision = BudgetDecision.BLOCK
    elif projected > safe_limit * warn_ratio:
        decision = BudgetDecision.WARN
    else:
        decision = BudgetDecision.ALLOW

    offer_chunked = (
        decision is not BudgetDecision.BLOCK and safe_file > safe_limit * chunked_file_ratio
    )
    reason = _reason_for(decision, offer_chunked=offer_chunked)

    return BudgetGateResult(
        decision=decision,
        offer_chunked=offer_chunked,
        baseline_tokens=safe_baseline,
        file_tokens=safe_file,
        projected_tokens=projected,
        limit_tokens=safe_limit,
        ratio_after=ratio_after,
        warn_ratio=warn_ratio,
        block_ratio=block_ratio,
        approximate=approximate,
        source=source,
        reason=reason,
    )


def _reason_for(decision: BudgetDecision, *, offer_chunked: bool) -> str:
    if decision is BudgetDecision.BLOCK:
        return _REASON_BLOCK
    if decision is BudgetDecision.WARN:
        return _REASON_WARN_CHUNKED if offer_chunked else _REASON_WARN
    return _REASON_ALLOW_CHUNKED if offer_chunked else _REASON_ALLOW


async def estimate_content_budget(
    content: str,
    *,
    baseline: int,
    limit: int,
    tokenizer: TokenizerClient,
    warn_ratio: float = DEFAULT_WARN_RATIO,
    block_ratio: float = DEFAULT_BLOCK_RATIO,
    chunked_file_ratio: float = DEFAULT_CHUNKED_FILE_RATIO,
) -> BudgetGateResult:
    """Estimate file tokens then run :func:`evaluate_budget`."""
    estimate = await tokenizer.estimate(content)
    return evaluate_budget(
        baseline=baseline,
        file_cost=estimate.n_tokens,
        limit=limit,
        warn_ratio=warn_ratio,
        block_ratio=block_ratio,
        chunked_file_ratio=chunked_file_ratio,
        approximate=estimate.approximate,
        source=estimate.source,
    )
