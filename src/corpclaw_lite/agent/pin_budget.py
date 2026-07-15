"""Pin context budget: sticky pins may use at most a fraction of the context window.

B-095 / DC-013: hard cap (default 25%) on sum of pinned file tokens per session.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_PIN_CONTEXT_RATIO",
    "PinBudgetResult",
    "evaluate_pin_budget",
    "pin_budget_limit",
]

DEFAULT_PIN_CONTEXT_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class PinBudgetResult:
    """Outcome of pin-slice budget check."""

    allowed: bool
    current_pinned_tokens: int
    new_tokens: int
    projected_tokens: int
    pin_budget: int
    pin_ratio: float
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "current_pinned_tokens": self.current_pinned_tokens,
            "new_tokens": self.new_tokens,
            "projected_tokens": self.projected_tokens,
            "pin_budget": self.pin_budget,
            "pin_ratio": self.pin_ratio,
            "reason": self.reason,
        }


def pin_budget_limit(context_limit: int, *, ratio: float = DEFAULT_PIN_CONTEXT_RATIO) -> int:
    """Return max tokens allowed for all pins combined."""
    safe_limit = max(1, int(context_limit))
    safe_ratio = min(0.5, max(0.05, float(ratio)))
    return max(1, int(safe_limit * safe_ratio))


def evaluate_pin_budget(
    *,
    current_pinned_tokens: int,
    new_tokens: int,
    context_limit: int,
    ratio: float = DEFAULT_PIN_CONTEXT_RATIO,
) -> PinBudgetResult:
    """Allow pin if current + new ≤ pin_budget (25% of context by default)."""
    budget = pin_budget_limit(context_limit, ratio=ratio)
    current = max(0, int(current_pinned_tokens))
    added = max(0, int(new_tokens))
    projected = current + added
    allowed = projected <= budget
    if allowed:
        reason = (
            f"Закрепление в пределах лимита ({projected}/{budget} токенов, "
            f"{int(ratio * 100)}% контекста)."
        )
    else:
        reason = (
            f"Закреплённые файлы не могут занимать больше {int(ratio * 100)}% контекста "
            f"({projected}/{budget} токенов). Снимите часть закреплений или уменьшите файл."
        )
    return PinBudgetResult(
        allowed=allowed,
        current_pinned_tokens=current,
        new_tokens=added,
        projected_tokens=projected,
        pin_budget=budget,
        pin_ratio=min(0.5, max(0.05, float(ratio))),
        reason=reason,
    )
