"""LLM-based memory consolidation — compresses old messages into summaries.

When conversation history exceeds a configurable threshold, the oldest half
is summarized via a single LLM call and replaced with a compact summary.
This keeps the context window manageable for local LLMs (8K–32K tokens).
"""

from __future__ import annotations

import logging
from typing import Any

from corpclaw_lite.llm.base import Provider
from corpclaw_lite.memory.sqlite import SQLiteMemory

__all__ = [
    "CONSOLIDATION_PROMPT",
    "MemoryConsolidator",
]

logger = logging.getLogger(__name__)

CONSOLIDATION_PROMPT = """Summarize the following conversation into 3-5 concise facts.
Focus on: key decisions made, important information shared, tasks completed or pending.
Write in the same language as the conversation. Be extremely concise.

Conversation:
{conversation}

Summary (3-5 bullet points):"""


class MemoryConsolidator:
    """Periodically compresses old conversation history via LLM summarization."""

    def __init__(self, provider: Provider, threshold: int = 30):
        self._provider = provider
        self._threshold = threshold

    async def maybe_consolidate(self, memory: SQLiteMemory, user_id: str) -> bool:
        """Check message count and consolidate if above threshold.

        Returns True if consolidation was performed.
        """
        count = await memory.count_messages(user_id)
        if count < self._threshold:
            return False

        split = count // 2
        history = await memory.get_history(user_id, limit=count)
        old_messages = history[:split]

        if not old_messages:
            return False

        try:
            summary = await self._summarize(old_messages)
            await memory.replace_oldest(user_id, count=split, summary=summary)
            logger.info(
                "Consolidated %d messages into summary for user %s",
                split,
                user_id,
            )
            return True
        except Exception as e:
            logger.error("Consolidation failed for user %s: %s", user_id, e)
            return False

    async def _summarize(self, messages: list[dict[str, Any]]) -> str:
        """Single LLM call to compress messages into bullet-point summary."""
        conversation_text = self._format_messages(messages)
        prompt = CONSOLIDATION_PROMPT.format(conversation=conversation_text)

        response = await self._provider.chat(
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content.strip()

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        """Format messages as readable text for the summarization prompt."""
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content_raw: Any = msg.get("content", "")
            # str(Any) is pyright-safe; handles str, dict, None, and all other types
            content: str = str(content_raw) if content_raw is not None else ""
            lines.append(f"{role}: {content}")
        return "\n".join(lines)
