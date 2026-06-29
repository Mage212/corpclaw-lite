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

        ``content`` must be a string — ``None`` breaks Qwen3.5's Jinja chat
        template (string concatenation with None raises a rendering error).
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
                "content": content if content is not None else "",
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

        Chat template compatibility (Qwen3.5, Ollama, LM Studio):
          - System messages are merged into the system prompt (not mid-conversation).
          - Leading assistant messages (e.g. consolidation summaries) are also
            merged into the system prompt — Qwen3.5 requires the first non-system
            message to be from the user role.
          - Tool-role messages from history are dropped (orphaned without tool_calls).

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

        # Phase 1: extract system messages from history → merge into system prompt.
        # (prevents mid-conversation system messages that break chat templates).
        history_system_parts: list[str] = []
        non_system_history: list[dict[str, Any]] = []
        for item in history or []:
            if item["role"] == "system":
                history_system_parts.append(str(item["content"]))
            else:
                non_system_history.append(item)

        if history_system_parts:
            system += "\n\n---\nExecution history:\n" + "\n".join(history_system_parts)

        # Phase 2: strip leading assistant messages from history → merge into
        # system prompt.  Qwen3.5 Jinja template requires the first non-system
        # message to be role=user; a leading assistant (e.g. consolidation
        # summary) breaks the template with "No user query found in messages."
        leading_assistant_parts: list[str] = []
        while non_system_history and non_system_history[0]["role"] == "assistant":
            leading_assistant_parts.append(str(non_system_history.pop(0)["content"]))

        if leading_assistant_parts:
            system += "\n\n---\nPrevious context:\n" + "\n".join(leading_assistant_parts)

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

        # Phase 3: add remaining history (user + assistant only, guaranteed to
        # start with user after phase 2).
        for item in non_system_history:
            role = item["role"]
            content_str = str(item["content"])
            if role == "user":
                builder.add_user_message(content_str)
            elif role == "assistant":
                builder.add_assistant_message(content_str)
            # Skip "system" (merged in phase 1) and "tool" (orphaned) roles
        builder.add_user_message(message)
        return builder

    @classmethod
    def build_from_full_history(
        cls,
        user: User,
        message: str,
        full_history: list[dict[str, Any]],
        system_prompt_override: str | None = None,
        few_shots: list[dict[str, Any]] | None = None,
    ) -> ContextBuilder:
        """Build context restoring the FULL LLM message schema including tool_calls
        and tool-role messages (B-063 S2: restore-on-activate).

        Unlike :meth:`build_initial`, this does NOT drop tool-role messages — it
        reconstructs them faithfully from the per-chat context store. System
        messages and leading assistant/tool messages are merged into the system
        prompt (chat-template safety, same as build_initial phases 1-2). Few-shot
        injection mirrors build_initial.

        Args:
            user: Current user.
            message: The current user message (appended last).
            full_history: Ordered messages from ``ChatContextStore.list_context``
                (role/content/tool_calls/tool_call_id/name).
            system_prompt_override: Assembled system prompt (base + department +
                onboarding + tone + user context + skills).
            few_shots: Calibrated few-shot examples (same format as
                ``build_initial``).
        """
        system = system_prompt_override or (
            f"You are CorpClaw Lite, a helpful assistant. "
            f"You are talking to {user.name} from the {user.department} department. "
            f"Use the available tools to help the user. If a tool returns an error, try to fix it."
        )

        # Merge system messages from history into the system prompt (same
        # rationale as build_initial phase 1: mid-conversation system messages
        # break chat templates).
        history_system_parts: list[str] = []
        non_system: list[dict[str, Any]] = []
        for item in full_history:
            if str(item.get("role")) == "system":
                history_system_parts.append(str(item.get("content", "")))
            else:
                non_system.append(item)
        if history_system_parts:
            system += "\n\n---\nExecution history:\n" + "\n".join(history_system_parts)

        # Phase 2: strip leading assistant AND tool messages → merge into system
        # prompt. Qwen3.5 Jinja template requires the first non-system message to
        # be role=user; a leading assistant/tool (e.g. a consolidation summary,
        # or an orphaned tool result after compression) breaks the template.
        leading_parts: list[str] = []
        while non_system and str(non_system[0].get("role")) in ("assistant", "tool"):
            item = non_system.pop(0)
            content = str(item.get("content", ""))
            calls = item.get("tool_calls")
            if calls:
                # B-063 final-fix B2: preserve tool_calls info in the merged
                # system text (can't emit structured tool_calls inside the system
                # prompt). Same rendering as few_shots tool_call description.
                calls_desc = ", ".join(
                    f"{c.get('function', {}).get('name', '?')}"
                    f"({c.get('function', {}).get('arguments', '')})"
                    for c in calls
                )
                content = f"{content} [Tool call: {calls_desc}]".strip()
            leading_parts.append(content)
        if leading_parts:
            system += "\n\n---\nPrevious context:\n" + "\n".join(leading_parts)

        builder = cls(system_prompt=system)

        # Inject few-shot examples before history (calibration support) — same
        # logic as build_initial, so restored chats keep the calibrated behavior.
        for shot in few_shots or []:
            user_msg = str(shot.get("user", ""))
            assistant_raw: Any = shot.get("assistant", {})
            if user_msg:
                builder.add_user_message(user_msg)
            if isinstance(assistant_raw, dict) and "content" in assistant_raw:
                builder.add_assistant_message(str(cast(str, assistant_raw["content"])))
            elif isinstance(assistant_raw, dict) and "tool_calls" in assistant_raw:
                raw_calls = cast(list[dict[str, Any]], assistant_raw["tool_calls"])
                calls_desc = ", ".join(
                    f"{tc.get('name', '?')}({tc.get('arguments', {})})" for tc in raw_calls
                )
                builder.add_assistant_message(f"[Tool call: {calls_desc}]")

        for item in non_system:
            role = str(item.get("role", "user"))
            content = str(item.get("content", ""))
            if role == "user":
                builder.add_user_message(content)
            elif role == "assistant":
                calls = item.get("tool_calls")
                if calls:
                    builder.add_tool_calls(_tool_calls_from_dicts(calls), content=content)
                else:
                    builder.add_assistant_message(content)
            elif role == "tool":
                builder.add_tool_result(
                    str(item.get("tool_call_id", "")),
                    str(item.get("name", "")),
                    content,
                )
            # Unknown roles are skipped (defensive).

        builder.add_user_message(message)
        return builder


def _tool_calls_from_dicts(calls: list[dict[str, Any]]) -> list[ToolCall]:
    """Convert context-store tool_call dicts → ToolCall models.

    The store serializes them in the OpenAI schema
    (``{id, type, function:{name, arguments:<json-string>}}``); the builder
    expects ``ToolCall`` instances.
    """
    out: list[ToolCall] = []
    for call in calls:
        fn_raw = call.get("function")
        fn: dict[str, Any] = cast(dict[str, Any], fn_raw) if isinstance(fn_raw, dict) else {}
        raw_args = fn.get("arguments", "{}")
        args: dict[str, Any]
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (json.JSONDecodeError, TypeError):
            args = {}
        out.append(
            ToolCall(
                id=str(call.get("id", "")),
                name=str(fn.get("name", "")),
                arguments=args,
            )
        )
    return out
