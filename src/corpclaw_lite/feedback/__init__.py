"""B-121 / DC-025a: user 👍/👎 feedback labels.

MVP scope (2026-07-27): binary up/down only, no comment; Telegram + Web both
covered. Each label carries ``run_id`` — the JOIN key against
``logs/llm_payloads.jsonl`` (LLM payload capture, §7.1.1). The dataset exporter
for fine-tuning (B-122) reads both sources and correlates them offline.
"""

from __future__ import annotations

from corpclaw_lite.feedback.models import FeedbackChannel, FeedbackLabel, FeedbackRating
from corpclaw_lite.feedback.store import VALID_CHANNELS, VALID_RATINGS, FeedbackStore

__all__ = [
    "FeedbackChannel",
    "FeedbackLabel",
    "FeedbackRating",
    "FeedbackStore",
    "VALID_CHANNELS",
    "VALID_RATINGS",
]
