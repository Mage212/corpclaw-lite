"""B-121 / DC-025a: data models for user 👍/👎 feedback labels.

MVP scope (2026-07-27): binary up/down only, no comment. Each label is
correlated with the LLM payload captures (``logs/llm_payloads.jsonl``) via
``run_id`` — the agent run identifier (``agent/loop.py:323``). JOIN is performed
offline by the dataset exporter (B-122).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "FeedbackLabel",
    "FeedbackRating",
    "FeedbackChannel",
]

# Binary rating. None is intentionally excluded — a record only exists once the
# user has tapped, and the rating is what they tapped.
FeedbackRating = str  # "up" | "down"

# Which channel the label came in through. Used for analytics + for the message
# reference semantics (Telegram = message_id, Web = None).
FeedbackChannel = str  # "telegram" | "web"


@dataclass(slots=True)
class FeedbackLabel:
    """One user's 👍/👎 on one agent run.

    Attributes:
        run_id: Agent run identifier — the JOIN key against
            ``logs/llm_payloads.jsonl`` records. Always present on labels that
            originate from a real agent run.
        user_id: Canonical user id (string — same type as User.id across the
            codebase, e.g. Telegram id as string, or web user id).
        rating: ``"up"`` or ``"down"``.
        channel: ``"telegram"`` or ``"web"``.
        message_ref: Telegram ``message_id`` (as string) for the message the
            buttons were attached to; ``None`` for the Web channel (the browser
            tracks its own message id locally).
        created_at: ISO timestamp (UTC, microsecond=0).
        updated_at: ISO timestamp of the most recent UPSERT (UTC). Equals
            ``created_at`` on first insert.
    """

    run_id: str
    user_id: str
    rating: FeedbackRating
    channel: FeedbackChannel
    message_ref: str | None
    created_at: str
    updated_at: str
