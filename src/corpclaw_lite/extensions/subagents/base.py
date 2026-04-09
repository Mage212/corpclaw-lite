from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "SubagentSpec",
]


@dataclass(frozen=True)
class SubagentSpec:
    """Specification for a subagent that can be dispatched by the main agent.

    Attributes:
        id: Unique identifier
        name: Human readable name
        description: Description of what this subagent is good at (used by main agent to pick it)
        capabilities: List of capability strings (mostly documentation)
        allowed_tools: List of tool names this subagent has access to
        prompt_path: Relative path to the system prompt for this subagent
    """

    id: str
    name: str
    description: str
    capabilities: list[str] = field(default_factory=lambda: [])
    allowed_tools: list[str] = field(default_factory=lambda: ["*"])
    prompt_path: str = ""
