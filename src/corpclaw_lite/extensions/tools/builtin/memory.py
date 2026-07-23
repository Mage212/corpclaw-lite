from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.context import get_tool_execution_context

__all__ = [
    "MemoryRecallTool",
    "MemoryStoreTool",
]

if TYPE_CHECKING:
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from corpclaw_lite.users.models import User


def _list_to_str_items(raw: Any) -> list[str]:
    """Coerce a JSON/list-like value to list[str] without pyright Unknown churn."""
    if not isinstance(raw, list):
        return []
    items = cast(list[Any], raw)
    return [str(x) for x in items]


def _parse_cues(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return _list_to_str_items(cast(Any, raw))
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.startswith("["):
        try:
            data: Any = json.loads(text)
            if isinstance(data, list):
                return _list_to_str_items(data)
        except json.JSONDecodeError:
            pass
    return [p.strip() for p in text.split(",") if p.strip()]


class MemoryStoreTool(Tool):
    """Store a long-term memory entry (Memora-style abstraction + value + cues)."""

    name = "memory_store"
    description = (
        "Store a fact about the user in long-term memory for future reference. "
        "Prefer abstraction (short 6–8 word hook) + value (full text) + optional cues "
        "(names, INN, project titles). Legacy key/value still accepted (key→abstraction)."
    )
    params = [
        ToolParam(
            name="abstraction",
            type="string",
            description="Short search hook, 6–8 words (e.g. 'Prefers brief replies')",
            required=False,
        ),
        ToolParam(
            name="value",
            type="string",
            description="Full fact text to remember",
            required=False,
        ),
        ToolParam(
            name="cues",
            type="string",
            description='Optional anchors as JSON array or comma list: ["Иванов","7707"]',
            required=False,
        ),
        ToolParam(
            name="key",
            type="string",
            description="Legacy: key/category (mapped to abstraction)",
            required=False,
        ),
    ]
    risk_level = RiskLevel.LOW

    def __init__(self, memory: SQLiteMemory) -> None:
        self._memory = memory

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        context = get_tool_execution_context()
        if user is None and context is not None:
            user = context.user
        if user is None:
            return "Error: User context is required for memory_store."

        abstraction_raw = kwargs.get("abstraction")
        value_raw = kwargs.get("value")
        key_raw = kwargs.get("key")

        abstraction: str | None = None
        if isinstance(abstraction_raw, str) and abstraction_raw.strip():
            abstraction = abstraction_raw.strip()
        elif isinstance(key_raw, str) and key_raw.strip():
            abstraction = key_raw.strip()

        value: str | None = None
        if isinstance(value_raw, str) and value_raw.strip():
            value = value_raw.strip()

        if abstraction is None or value is None:
            return "Error: require (abstraction + value) or legacy (key + value) string parameters."

        cues = _parse_cues(kwargs.get("cues"))
        await self._memory.store_entry(
            str(user.id),
            primary_abstraction=abstraction,
            memory_value=value,
            cues=cues,
        )
        cue_note = f" cues={cues}" if cues else ""
        return f"Stored: {abstraction} = {value}{cue_note}"


class MemoryRecallTool(Tool):
    """Recall stored memory entries (hybrid FTS + cue match)."""

    name = "memory_recall"
    description = (
        "Recall stored facts about the user from long-term memory. "
        "Optionally filter by a search query (matches abstraction, value, and cues)."
    )
    params = [
        ToolParam(
            name="query",
            type="string",
            description="Optional search filter for abstraction, value, and cues",
            required=False,
        ),
    ]
    risk_level = RiskLevel.LOW

    def __init__(self, memory: SQLiteMemory) -> None:
        self._memory = memory

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        context = get_tool_execution_context()
        if user is None and context is not None:
            user = context.user
        query: str | None = kwargs.get("query")

        if user is None:
            return "Error: User context is required for memory_recall."

        if isinstance(query, str) and not query.strip():
            query = None

        entries = await self._memory.recall_entries(str(user.id), query)

        if not entries and query:
            entries = await self._memory.recall_entries(str(user.id), None)
            if entries:
                return (
                    f"No entries matched '{query}' directly, "
                    f"but here are all stored entries:\n" + _format_entries(entries)
                )

        if not entries:
            return "No facts stored."

        return "Stored facts:\n" + _format_entries(entries)


def _format_entries(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for e in entries:
        abs_ = str(e.get("abstraction") or e.get("key") or "")
        val = str(e.get("value") or "")
        lines.append(f"- {abs_}: {val}")
        cues_raw = e.get("cues")
        if isinstance(cues_raw, list) and cues_raw:
            lines.append(f"  cues: {', '.join(_list_to_str_items(cast(Any, cues_raw)))}")
    return "\n".join(lines)
