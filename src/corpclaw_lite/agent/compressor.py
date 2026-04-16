"""Context window compression for long conversations.

Adapted from Hermes Agent's ContextCompressor for async CorpClaw Lite.
Key differences:
- Async (uses our Provider protocol for LLM summarization)
- Integrated with CorpClaw security context (department-aware)
- Simpler (no auxiliary client, no provider-specific logic)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from corpclaw_lite.agent.constants import PLACEHOLDER
from corpclaw_lite.config.settings import CompressionSettings

__all__ = [
    "ContextCompressor",
    "PLACEHOLDER",
]

if TYPE_CHECKING:
    from corpclaw_lite.llm.base import Provider

logger = logging.getLogger(__name__)

_MAX_TRACKED_USERS = 5000


class ContextCompressor:
    """Multi-phase context compression to fit long conversations in context window."""

    def __init__(self, provider: Provider, settings: CompressionSettings):
        self._provider = provider
        self._settings = settings
        self._summaries: dict[str, str] = {}

    def _prune_summaries(self) -> None:
        """Evict oldest half of per-user summaries when capacity exceeded."""
        if len(self._summaries) <= _MAX_TRACKED_USERS:
            return
        sorted_keys = sorted(self._summaries.keys())
        to_remove = sorted_keys[: len(sorted_keys) - _MAX_TRACKED_USERS // 2]
        for k in to_remove:
            del self._summaries[k]

    def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        """Check if compression is needed based on token threshold.

        Returns False if a tool result is the last message — compressing mid-ReAct
        iteration (between tool execution and the next LLM call) corrupts the agent
        context and causes tasks to be abandoned.
        """
        if not self._settings.enabled:
            return False

        # Safety guard: never compress while a tool call is still "in flight".
        # The last message being a tool result means the agent hasn't yet processed
        # the output — compressing here would produce a summary *instead of* the
        # actual answer and lose the task context entirely.
        if messages and messages[-1].get("role") == "tool":
            logger.debug(
                "ContextCompressor: skipping compression — last message is a pending tool result"
            )
            return False

        tokens = self._estimate_tokens(messages)
        threshold = int(self._settings.max_context_tokens * self._settings.threshold_ratio)
        should = tokens >= threshold
        if should:
            logger.info(
                "ContextCompressor: compression needed (tokens=%d, threshold=%d)",
                tokens,
                threshold,
            )
        return should

    async def compress(
        self, messages: list[dict[str, Any]], mem_key: str | None = None
    ) -> list[dict[str, Any]]:
        """Multi-phase compression pipeline.

        1. Prune old tool results (cheap, no LLM)
        2. Protect head (system + first exchange)
        3. Protect tail by token budget
        4. Summarize middle with structured prompt
        5. Sanitize tool pairs (fix orphaned tool_call/result)
        """
        if len(messages) < 5:
            return messages

        # Note: prune_old_tool_results is already called by the loop before compress(),
        # so we skip it here to avoid double-pruning.

        tokens = self._estimate_tokens(messages)
        tail_budget = self._settings.protect_tail_tokens
        head_count = 2

        if tokens <= self._settings.max_context_tokens:
            return self._sanitize_tool_pairs(messages)

        tail_start = self._find_tail_boundary(messages, tail_budget)
        # head_count + 2: require at least 2 messages in middle for compression to be worthwhile
        if tail_start <= head_count + 2:
            logger.warning(
                "ContextCompressor: tail protection covers most of context, skipping compression"
            )
            return self._sanitize_tool_pairs(messages)

        head = messages[:head_count]
        middle = messages[head_count:tail_start]
        tail = messages[tail_start:]

        if middle:
            summary = await self._generate_summary(middle, mem_key)
            if summary:
                summary_msg: dict[str, Any] = {
                    "role": "user",
                    "content": f"[Context Summary]\n{summary}",
                }
                result = head + [summary_msg] + tail
            else:
                result = head + tail
        else:
            result = head + tail

        result = self._sanitize_tool_pairs(result)
        logger.info(
            "ContextCompressor: compressed %d messages to %d",
            len(messages),
            len(result),
        )
        return result

    def _sanitize_tool_pairs(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fix orphaned tool_call/result pairs.

        Two cases:
        1. Orphaned tool result (no matching tool_call) → remove
        2. Orphaned tool call (no matching result) → add stub result
        """
        tool_call_ids: set[str] = set()
        tool_result_ids: set[str] = set()

        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id:
                        tool_call_ids.add(tc_id)
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    tool_result_ids.add(tc_id)

        orphaned_results = tool_result_ids - tool_call_ids
        orphaned_calls = tool_call_ids - tool_result_ids

        if not orphaned_results and not orphaned_calls:
            return messages

        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id") in orphaned_results:
                continue
            result.append(msg)

            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id and tc_id in orphaned_calls:
                        stub: dict[str, Any] = {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": tc.get("function", {}).get("name", "unknown"),
                            "content": "[Tool result was lost during context compression]",
                        }
                        result.append(stub)

        logger.debug(
            "ContextCompressor: sanitized %d orphaned results, %d orphaned calls",
            len(orphaned_results),
            len(orphaned_calls),
        )
        return result

    async def _generate_summary(
        self, turns: list[dict[str, Any]], mem_key: str | None = None
    ) -> str | None:
        """Generate structured summary of conversation turns using LLM."""
        turns_text = self._format_turns_for_summary(turns)
        if not turns_text.strip():
            return None

        prev_summary = mem_key and self._summaries.get(mem_key)
        prev = f"\n\nPrevious summary:\n{prev_summary}\n" if prev_summary else ""

        prompt = f"""Summarize the following conversation history. Be concise and structured.

Format your response as:
**Goal:** [What the user is trying to accomplish]
**Progress:** [What has been done so far]
**Key Decisions:** [Important choices made]
**Files Involved:** [Any files mentioned]
**Next Steps:** [What likely needs to happen next]
{prev}
Conversation to summarize:
{turns_text}

Summary:"""

        try:
            from corpclaw_lite.llm.router import LLMRouter

            effective_provider = self._provider
            if isinstance(self._provider, LLMRouter):
                effective_provider = self._provider.for_task("compress")

            response = await effective_provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
            )
            summary = response.content
            if summary and mem_key:
                self._summaries[mem_key] = summary.strip()
                self._prune_summaries()
            return summary
        except Exception as e:
            logger.warning("ContextCompressor: summary generation failed: %s", e)
            return None

    def _format_turns_for_summary(self, turns: list[dict[str, Any]], max_chars: int = 8000) -> str:
        """Format turns for summary prompt, truncating if needed."""
        lines: list[str] = []
        total = 0

        for msg in turns:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if role == "tool":
                name = msg.get("name", "tool")
                if content == PLACEHOLDER or len(str(content)) > 300:
                    content = f"[{name} output truncated]"
            elif role == "assistant" and msg.get("tool_calls"):
                tools = ", ".join(
                    tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"]
                )
                content = f"[Called tools: {tools}] {content or ''}"

            if content:
                line = f"{role}: {content}"
                if total + len(line) > max_chars:
                    break
                lines.append(line)
                total += len(line)

        return "\n".join(lines)

    def _find_tail_boundary(self, messages: list[dict[str, Any]], tail_budget_tokens: int) -> int:
        """Find where tail protection should start to preserve token budget."""
        tokens = 0
        for i in range(len(messages) - 1, -1, -1):
            tokens += self._estimate_tokens([messages[i]])
            if tokens >= tail_budget_tokens:
                return i
        return 0

    @staticmethod
    def _bytes_to_tokens(text: str) -> int:
        """Estimate token count for a string.

        - Mostly ASCII (English): len_bytes / 4 ≈ tokens (accurate for BPE)
        - Non-ASCII heavy (Cyrillic/CJK): len_bytes / 2 (conservative — prevents
          underestimation that causes unexpected context limit hits)
        """
        encoded = text.encode("utf-8")
        # ratio > 1.3 means significant non-ASCII content
        divisor = 2 if len(encoded) > len(text) * 1.3 else 4
        return len(encoded) // max(divisor, 1)

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough token estimate using utf-8 byte count heuristic."""
        total = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total += self._bytes_to_tokens(content)
            elif content is not None:
                total += self._bytes_to_tokens(str(content))

            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    args = tc.get("function", {}).get("arguments", "")
                    if isinstance(args, str):
                        total += self._bytes_to_tokens(args)
                    elif args:
                        total += self._bytes_to_tokens(str(args))

        return total
