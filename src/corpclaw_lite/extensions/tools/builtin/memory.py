from __future__ import annotations

from typing import TYPE_CHECKING, Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

if TYPE_CHECKING:
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from corpclaw_lite.users.models import User


class MemoryStoreTool(Tool):
    """Store a key-value fact about the user in long-term memory."""

    name = "memory_store"
    description = "Store a fact about the user in long-term memory for future reference."
    params = [
        ToolParam(
            name="key",
            type="string",
            description="Key or category (e.g. 'name', 'preference', 'project')",
        ),
        ToolParam(
            name="value",
            type="string",
            description="The fact value to remember",
        ),
    ]
    risk_level = RiskLevel.LOW

    def __init__(self, memory: SQLiteMemory) -> None:
        self._memory = memory

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        key = kwargs.get("key")
        value = kwargs.get("value")

        if not isinstance(key, str) or not isinstance(value, str):
            return "Error: 'key' and 'value' are required string parameters."

        if user is None:
            return "Error: User context is required for memory_store."

        await self._memory.store_fact(str(user.id), key, value)
        return f"Stored: {key} = {value}"


class MemoryRecallTool(Tool):
    """Recall stored facts about the user from long-term memory."""

    name = "memory_recall"
    description = (
        "Recall stored facts about the user from long-term memory. "
        "Optionally filter by a search query."
    )
    params = [
        ToolParam(
            name="query",
            type="string",
            description="Optional search filter for keys and values",
            required=False,
        ),
    ]
    risk_level = RiskLevel.LOW

    def __init__(self, memory: SQLiteMemory) -> None:
        self._memory = memory

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        query: str | None = kwargs.get("query")

        if user is None:
            return "Error: User context is required for memory_recall."

        # Normalize empty string to None
        if isinstance(query, str) and not query.strip():
            query = None

        facts = await self._memory.recall_facts(str(user.id), query)
        if not facts:
            return "No facts stored." if not query else f"No facts matching '{query}'."

        lines = [f"- {f['key']}: {f['value']}" for f in facts]
        return "Stored facts:\n" + "\n".join(lines)
