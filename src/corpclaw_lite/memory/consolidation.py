"""LLM-based memory consolidation — compresses old messages into summaries.

When conversation history exceeds a configurable threshold, the oldest half
is summarized via a single LLM call and replaced with a compact summary.
This keeps the context window manageable for local LLMs (8K–32K tokens).

Safety:
    - Consolidation is skipped if the tail of the history contains tool-related
      content markers (e.g. "[Called tools:" or "[Tool result"), indicating an
      active workflow.
    - A cooldown timer prevents repeated consolidation within a single rapid
      exchange (default: 60 seconds between consolidation attempts per user).
"""

from __future__ import annotations

import logging
import time
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

# Content markers that indicate active workflow — consolidation must be skipped
_ACTIVE_WORKFLOW_MARKERS = (
    "[Called tools:",
    "[Tool result",
    "[tool output truncated]",
    "Error executing tool",
)

# Minimum seconds between consolidation attempts for each user
_COOLDOWN_SECONDS = 60


class MemoryConsolidator:
    """Periodically compresses old conversation history via LLM summarization."""

    def __init__(self, provider: Provider, threshold: int = 30):
        self._provider = provider
        self._threshold = threshold
        # Per-user cooldown: {user_id: last_consolidation_time}
        self._last_consolidated: dict[str, float] = {}

    async def maybe_consolidate(self, memory: SQLiteMemory, user_id: str) -> bool:
        """Check message count and consolidate if above threshold.

        Returns True if consolidation was performed.

        Safety guardrails:
            1. Message count must exceed the threshold.
            2. Cooldown: at least _COOLDOWN_SECONDS since the last consolidation
               for this user (prevents rapid-fire during active sessions).
            3. Content guard: the last 6 messages must NOT contain tool-related
               markers — those indicate an active workflow whose context must
               not be destroyed.
        """
        count = await memory.count_messages(user_id)
        if count < self._threshold:
            return False

        # Cooldown: don't consolidate more than once per minute per user.
        now = time.monotonic()
        last = self._last_consolidated.get(user_id, 0.0)
        if now - last < _COOLDOWN_SECONDS:
            logger.debug(
                "Consolidation skipped for user %s — cooldown (%ds remaining)",
                user_id,
                int(_COOLDOWN_SECONDS - (now - last)),
            )
            return False

        # Load full history to inspect tail for active-workflow signals
        history = await memory.get_history(user_id, limit=count)

        # Content guard: scan last 6 messages for tool-related markers.
        # Since SQLiteMemory.get_history() stores only role+content (no tool_calls),
        # we detect active workflows by looking for formatted tool markers that
        # the ContextCompressor / ContextBuilder inject into content strings.
        tail_window = history[-6:] if len(history) >= 6 else history
        for msg in tail_window:
            content = str(msg.get("content", ""))
            for marker in _ACTIVE_WORKFLOW_MARKERS:
                if marker in content:
                    logger.debug(
                        "Consolidation skipped for user %s — active workflow marker '%s' in tail",
                        user_id,
                        marker,
                    )
                    return False

        split = count // 2
        old_messages = history[:split]

        if not old_messages:
            return False

        try:
            summary = await self._summarize(old_messages)
            await memory.replace_oldest(user_id, count=split, summary=summary)
            self._last_consolidated[user_id] = time.monotonic()
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

