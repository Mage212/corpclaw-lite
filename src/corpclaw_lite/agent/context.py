from __future__ import annotations

import json
from typing import Any

from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import ToolCall
from corpclaw_lite.users.models import User


class ContextBuilder:
    """Builds LLM context from history, system prompts, and tool results."""

    def __init__(self, system_prompt: str = ""):
        self.system_prompt = system_prompt
        self.messages: list[dict[str, Any]] = []
        if self.system_prompt:
            self.messages.append({"role": "system", "content": self.system_prompt})

    def add_user_message(self, content: str) -> None:
        """Add a user message to context."""
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        """Add an assistant text message."""
        self.messages.append({"role": "assistant", "content": content})

    def add_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        """Add tool calls made by the assistant."""
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
                "content": None,
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

    @classmethod
    def build_initial(cls, user: User, message: str, registry: ToolRegistry) -> ContextBuilder:
        """Build the initial context for a new user message."""
        # For Phase 1, just a hardcoded basic system prompt
        system = (
            f"You are CorpClaw Lite, a helpful assistant. "
            f"You are talking to {user.name} from the {user.department} department. "
            f"Use the available tools to help the user. If a tool returns an error, try to fix it."
        )
        builder = cls(system_prompt=system)
        builder.add_user_message(message)
        return builder
