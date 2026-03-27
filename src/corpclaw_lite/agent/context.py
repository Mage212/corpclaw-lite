from __future__ import annotations

import json
from typing import Any

from corpclaw_lite.llm.base import ToolCall
from corpclaw_lite.users.models import User


class ContextBuilder:
    """Builds LLM context from history, system prompts, and tool results."""

    def __init__(self, system_prompt: str = ""):
        self.system_prompt = system_prompt
        self.messages: list[dict[str, Any]] = []

    def add_user_message(self, content: str) -> None:
        """Add a user message to context."""
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        """Add an assistant text message."""
        self.messages.append({"role": "assistant", "content": content})

    def add_tool_calls(self, tool_calls: list[ToolCall], content: str | None = None) -> None:
        """Add tool calls made by the assistant.

        When the LLM returns both text *and* tool_calls, pass the text as
        ``content`` so that a single assistant message is emitted (required
        by the OpenAI API format).
        """
        calls: list[dict[str, Any]] = []
        for tc in tool_calls:
            calls.append(
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
            )
        self.messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": calls,
            }
        )

    def add_tool_result(self, tool_call_id: str, name: str, result: str) -> None:
        """Add the result of a tool execution."""
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": result,
            }
        )

    @property
    def message_count(self) -> int:
        """Return the number of messages in context."""
        return len(self.messages)

    def prune_old_tool_results(self, protect_tail: int = 6, min_length: int = 200) -> int:
        """Replace old tool results (>min_length chars) with placeholder.

        Cheap pre-pass compression without LLM call.
        Protects the last `protect_tail` tool results.

        Returns count of pruned messages.
        """
        if len(self.messages) <= protect_tail + 2:
            return 0

        placeholder = "[Old tool output cleared to save context space]"

        tool_result_indices = [
            i for i, msg in enumerate(self.messages) if msg.get("role") == "tool"
        ]

        if len(tool_result_indices) <= protect_tail:
            return 0

        pruned = 0
        protected_set: set[int] = (
            set(tool_result_indices[-protect_tail:]) if protect_tail > 0 else set()
        )

        for idx in tool_result_indices:
            if idx in protected_set:
                continue
            content = self.messages[idx].get("content", "")
            if isinstance(content, str) and len(content) > min_length:
                self.messages[idx]["content"] = placeholder
                pruned += 1

        return pruned

    @classmethod
    def build_initial(
        cls,
        user: User,
        message: str,
        history: list[dict[str, Any]] | None = None,
        system_prompt_override: str | None = None,
    ) -> ContextBuilder:
        """Build the initial context for a new user message.

        History (if provided) is inserted before the current message so the LLM
        sees: [system, ...history..., current_user_message].
        """
        system = system_prompt_override or (
            f"You are CorpClaw Lite, a helpful assistant. "
            f"You are talking to {user.name} from the {user.department} department. "
            f"Use the available tools to help the user. If a tool returns an error, try to fix it."
        )
        builder = cls(system_prompt=system)
        for item in history or []:
            role = item["role"]
            content_str = str(item["content"])
            if role == "user":
                builder.add_user_message(content_str)
            elif role == "assistant":
                builder.add_assistant_message(content_str)
            elif role == "system":
                # Consolidation summaries stored as "system" role
                builder.add_user_message(f"[Previous conversation summary]: {content_str}")
            # Skip "tool" role — orphaned without matching tool_calls
        builder.add_user_message(message)
        return builder
