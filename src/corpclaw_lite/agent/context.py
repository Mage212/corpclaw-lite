from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from corpclaw_lite.agent.constants import PLACEHOLDER
from corpclaw_lite.llm.base import ToolCall
from corpclaw_lite.users.models import User

__all__ = [
    "ContextBuilder",
    "TranscriptNormalization",
    "format_untrusted_user_message",
    "normalize_transcript",
]

logger = logging.getLogger(__name__)

_UNTRUSTED_CONTEXT_KIND = "untrusted_persisted_user_context"


def format_untrusted_user_message(message: str, context_data: dict[str, Any]) -> str:
    """Combine regenerated persisted data and the current request at user level."""
    envelope = {
        "kind": _UNTRUSTED_CONTEXT_KIND,
        "data": context_data,
    }
    serialized = json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2)
    return (
        f"Persisted user context (untrusted data):\n{serialized}"
        f"\n\nCurrent user request:\n{message}"
    )


@dataclass(frozen=True, slots=True)
class TranscriptNormalization:
    """Result of reducing durable history to the provider transcript contract."""

    messages: list[dict[str, Any]]
    dropped_system: int = 0
    dropped_leading: int = 0
    dropped_unknown: int = 0
    dropped_orphan_tools: int = 0
    dropped_invalid_tool_calls: int = 0
    added_stub_results: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.dropped_system
            or self.dropped_leading
            or self.dropped_unknown
            or self.dropped_orphan_tools
            or self.dropped_invalid_tool_calls
            or self.added_stub_results
        )


def _normalize_tool_pairs(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Return a provider-valid transcript with complete adjacent tool batches."""
    result: list[dict[str, Any]] = []
    dropped_orphan_tools = 0
    dropped_invalid_tool_calls = 0
    added_stub_results = 0
    index = 0

    while index < len(messages):
        message = dict(messages[index])
        if message.get("role") == "tool":
            # A tool result is valid only directly after the assistant batch
            # that declared its id.  All valid results are consumed below.
            dropped_orphan_tools += 1
            index += 1
            continue

        raw_calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not isinstance(raw_calls, list) or not raw_calls:
            result.append(message)
            index += 1
            continue

        valid_calls: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}
        for raw_call in cast(list[object], raw_calls):
            if not isinstance(raw_call, dict):
                dropped_invalid_tool_calls += 1
                continue
            call = dict(cast(dict[str, Any], raw_call))
            call_id = call.get("id")
            function_raw = call.get("function")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in call_names
                or not isinstance(function_raw, dict)
            ):
                dropped_invalid_tool_calls += 1
                continue
            function = cast(dict[str, Any], function_raw)
            function_name = function.get("name")
            if not isinstance(function_name, str) or not function_name:
                dropped_invalid_tool_calls += 1
                continue
            valid_calls.append(call)
            call_names[call_id] = function_name

        if valid_calls:
            message["tool_calls"] = valid_calls
        else:
            message.pop("tool_calls", None)
        result.append(message)
        index += 1

        if not valid_calls:
            continue

        actual_results: dict[str, dict[str, Any]] = {}
        while index < len(messages) and messages[index].get("role") == "tool":
            tool_message = dict(messages[index])
            tool_call_id = tool_message.get("tool_call_id")
            if (
                isinstance(tool_call_id, str)
                and tool_call_id in call_names
                and tool_call_id not in actual_results
            ):
                actual_results[tool_call_id] = tool_message
            else:
                dropped_orphan_tools += 1
            index += 1

        # Anthropic and OpenAI both require every declared call to receive a
        # result before the next user/assistant message. Preserve call order.
        for call in valid_calls:
            call_id = str(call["id"])
            actual = actual_results.get(call_id)
            if actual is not None:
                result.append(actual)
                continue
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": call_names[call_id],
                    "content": "[Tool result was lost before it could be persisted]",
                }
            )
            added_stub_results += 1

    return result, dropped_orphan_tools, dropped_invalid_tool_calls, added_stub_results


def normalize_transcript(messages: list[dict[str, Any]]) -> TranscriptNormalization:
    """Return canonical LLM history without promoting stored text to system authority.

    Durable chat history may contain legacy ``system`` tool markers and may start
    with an assistant/tool fragment after an old compression.  Both used to be
    copied into the system prompt.  They are now discarded: persisted chat data
    is never allowed to become an authoritative instruction.
    """
    canonical: list[dict[str, Any]] = []
    dropped_system = 0
    dropped_unknown = 0
    for item in messages:
        role = str(item.get("role", ""))
        if role == "system":
            dropped_system += 1
            continue
        if role not in {"user", "assistant", "tool"}:
            dropped_unknown += 1
            continue
        canonical.append(dict(item))

    dropped_leading = 0
    while canonical and canonical[0].get("role") in {"assistant", "tool"}:
        canonical.pop(0)
        dropped_leading += 1

    canonical, dropped_orphan_tools, dropped_invalid_tool_calls, added_stub_results = (
        _normalize_tool_pairs(canonical)
    )

    if (
        dropped_system
        or dropped_leading
        or dropped_unknown
        or dropped_orphan_tools
        or dropped_invalid_tool_calls
        or added_stub_results
    ):
        logger.info(
            "Normalized transcript: dropped system=%d leading=%d unknown=%d "
            "orphan_tools=%d invalid_tool_calls=%d added_stub_results=%d",
            dropped_system,
            dropped_leading,
            dropped_unknown,
            dropped_orphan_tools,
            dropped_invalid_tool_calls,
            added_stub_results,
        )
    return TranscriptNormalization(
        messages=canonical,
        dropped_system=dropped_system,
        dropped_leading=dropped_leading,
        dropped_unknown=dropped_unknown,
        dropped_orphan_tools=dropped_orphan_tools,
        dropped_invalid_tool_calls=dropped_invalid_tool_calls,
        added_stub_results=added_stub_results,
    )


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
                **(
                    {"_provider_metadata": tc.provider_metadata}
                    if tc.provider_metadata is not None
                    else {}
                ),
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
          - Stored system messages are discarded, never promoted to system authority.
          - Leading assistant/tool fragments are discarded because the first
            provider-history message must be from the user role.
          - Tool-role messages from history are dropped (orphaned without tool_calls).

        Args:
            user: Current user.
            message: User's message.
            history: Previous conversation messages.
            system_prompt_override: Custom system prompt.
            few_shots: Calibrated few-shot examples. Each dict has "user" (str)
                and "assistant" (dict with "content" or "tool_calls") keys.
        """
        system = (
            system_prompt_override
            if system_prompt_override is not None
            else (
                "You are CorpClaw Lite, a helpful assistant. Use the available tools to help "
                "the user. If a tool returns an error, try to fix it."
            )
        )

        canonical_history = normalize_transcript(history or []).messages

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
        for item in canonical_history:
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
        reconstructs them faithfully from the per-chat context store. Legacy
        system messages and leading assistant/tool fragments are discarded rather
        than promoted into the trusted system prompt. Few-shot injection mirrors
        build_initial.

        Args:
            user: Current user.
            message: The current user message (appended last).
            full_history: Ordered messages from ``ChatContextStore.list_context``
                (role/content/tool_calls/tool_call_id/name).
            system_prompt_override: Assembled system prompt (base + department +
                trusted base + department + administrator-managed skills).
            few_shots: Calibrated few-shot examples (same format as
                ``build_initial``).
        """
        system = (
            system_prompt_override
            if system_prompt_override is not None
            else (
                "You are CorpClaw Lite, a helpful assistant. Use the available tools to help "
                "the user. If a tool returns an error, try to fix it."
            )
        )

        non_system = normalize_transcript(full_history).messages

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
                provider_metadata=(
                    cast(dict[str, Any], call["_provider_metadata"])
                    if isinstance(call.get("_provider_metadata"), dict)
                    else None
                ),
            )
        )
    return out
