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

from corpclaw_lite.config.settings import CompressionSettings

if TYPE_CHECKING:
    from corpclaw_lite.llm.base import Provider

logger = logging.getLogger(__name__)

PLACEHOLDER = "[Old tool output cleared to save context space]"


class ContextCompressor:
    """Multi-phase context compression to fit long conversations in context window."""

    def __init__(self, provider: Provider, settings: CompressionSettings):
        self._provider = provider
        self._settings = settings
        self._previous_summary: str | None = None

    def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        """Check if compression is needed based on token threshold."""
        if not self._settings.enabled:
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

    async def compress(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Multi-phase compression pipeline.

        1. Prune old tool results (cheap, no LLM)
        2. Protect head (system + first exchange)
        3. Protect tail by token budget
        4. Summarize middle with structured prompt
        5. Sanitize tool pairs (fix orphaned tool_call/result)
        """
        if len(messages) < 5:
            return messages

        messages = self._prune_old_tool_results(messages, protect_tail_count=6)

        tokens = self._estimate_tokens(messages)
        tail_budget = self._settings.protect_tail_tokens
        head_count = 2

        if tokens <= self._settings.max_context_tokens:
            return self._sanitize_tool_pairs(messages)

        tail_start = self._find_tail_boundary(messages, tail_budget)
        if tail_start <= head_count + 2:
            logger.warning(
                "ContextCompressor: tail protection covers most of context, skipping compression"
            )
            return self._sanitize_tool_pairs(messages)

        head = messages[:head_count]
        middle = messages[head_count:tail_start]
        tail = messages[tail_start:]

        if middle:
            summary = await self._generate_summary(middle)
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

    def _prune_old_tool_results(
        self, messages: list[dict[str, Any]], protect_tail_count: int = 6, min_length: int = 200
    ) -> list[dict[str, Any]]:
        """Replace old tool results with placeholder (cheap, no LLM)."""
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_indices) <= protect_tail_count:
            return messages

        protected: set[int] = (
            set(tool_indices[-protect_tail_count:]) if protect_tail_count > 0 else set()
        )
        result = []
        pruned = 0

        for i, msg in enumerate(messages):
            if msg.get("role") == "tool" and i not in protected:
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > min_length:
                    result.append({**msg, "content": PLACEHOLDER})
                    pruned += 1
                else:
                    result.append(msg)
            else:
                result.append(msg)

        if pruned:
            logger.debug("ContextCompressor: pruned %d old tool results", pruned)
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

        result = []
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

    async def _generate_summary(self, turns: list[dict[str, Any]]) -> str | None:
        """Generate structured summary of conversation turns using LLM."""
        turns_text = self._format_turns_for_summary(turns)
        if not turns_text.strip():
            return None

        prev = (
            f"\n\nPrevious summary:\n{self._previous_summary}\n" if self._previous_summary else ""
        )

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
            response = await self._provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
            )
            summary = response.content
            if summary:
                self._previous_summary = summary.strip()
            return summary
        except Exception as e:
            logger.warning("ContextCompressor: summary generation failed: %s", e)
            return None

    def _format_turns_for_summary(self, turns: list[dict[str, Any]], max_chars: int = 8000) -> str:
        """Format turns for summary prompt, truncating if needed."""
        lines = []
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

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough token estimate using len/4 heuristic."""
        total = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content) // 4
            elif content is not None:
                total += len(str(content)) // 4

            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    args = tc.get("function", {}).get("arguments", "")
                    if isinstance(args, str):
                        total += len(args) // 4
                    elif args:
                        total += len(str(args)) // 4

        return total
