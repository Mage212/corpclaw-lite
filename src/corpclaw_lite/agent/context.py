from __future__ import annotations

import json
from typing import Any, cast

from corpclaw_lite.agent.constants import PLACEHOLDER
from corpclaw_lite.llm.base import ToolCall
from corpclaw_lite.users.models import User

__all__ = [
    "ContextBuilder",
]


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
        calls = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in tool_calls
        ]
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
                self.messages[idx]["content"] = PLACEHOLDER
                pruned += 1

        return pruned

    @classmethod
    def build_initial(
        cls,
        user: User,
        message: str,
        history: list[dict[str, Any]] | None = None,
        system_prompt_override: str | None = None,
        few_shots: list[dict[str, Any]] | None = None,
    ) -> ContextBuilder:
        """Build the initial context for a new user message.

        History (if provided) is inserted before the current message so the LLM
        sees: [system, ...few_shots..., ...history..., current_user_message].

        System messages from history (execution records, consolidation summaries)
        are **merged into the system prompt** rather than inserted mid-conversation.
        Many LLM chat templates (Ollama/Qwen) only accept system messages at
        position 0 — placing them elsewhere causes "Channel Error".

        Args:
            user: Current user.
            message: User's message.
            history: Previous conversation messages.
            system_prompt_override: Custom system prompt.
            few_shots: Calibrated few-shot examples. Each dict has "user" (str)
                and "assistant" (dict with "content" or "tool_calls") keys.
        """
        system = system_prompt_override or (
            f"You are CorpClaw Lite, a helpful assistant. "
            f"You are talking to {user.name} from the {user.department} department. "
            f"Use the available tools to help the user. If a tool returns an error, try to fix it."
        )

        # Extract system messages from history and merge into system prompt
        # (prevents mid-conversation system messages that break chat templates).
        history_system_parts: list[str] = []
        filtered_history: list[dict[str, Any]] = []
        for item in history or []:
            if item["role"] == "system":
                history_system_parts.append(str(item["content"]))
            else:
                filtered_history.append(item)

        if history_system_parts:
            system += "\n\n---\nExecution history:\n" + "\n".join(history_system_parts)

        builder = cls(system_prompt=system)

        # Inject few-shot examples before history (calibration support)
        for shot in few_shots or []:
            user_msg = str(shot.get("user", ""))
            assistant_raw: Any = shot.get("assistant", {})
            if user_msg:
                builder.add_user_message(user_msg)
            if isinstance(assistant_raw, dict) and "content" in assistant_raw:
                builder.add_assistant_message(str(cast(str, assistant_raw["content"])))
            elif isinstance(assistant_raw, dict) and "tool_calls" in assistant_raw:
                # Simplified: show tool call as assistant text for pattern matching
                raw_calls = cast(list[dict[str, Any]], assistant_raw["tool_calls"])
                calls_desc = ", ".join(
                    f"{tc.get('name', '?')}({tc.get('arguments', {})})" for tc in raw_calls
                )
                builder.add_assistant_message(f"[Tool call: {calls_desc}]")

        for item in filtered_history:
            role = item["role"]
            content_str = str(item["content"])
            if role == "user":
                builder.add_user_message(content_str)
            elif role == "assistant":
                builder.add_assistant_message(content_str)
            # Skip "system" (merged above) and "tool" (orphaned) roles
        builder.add_user_message(message)
        return builder
