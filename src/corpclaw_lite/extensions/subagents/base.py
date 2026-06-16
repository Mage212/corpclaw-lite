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
        direct_response: Whether dispatch_subagent should return this result directly to the user
        max_wall_time_ms: Per-subagent wall-clock budget override (B-049). When set, the
            subagent's inner AgentLoop and the dispatcher's asyncio.wait_for timeout use
            this value instead of the global AgentSettings.max_wall_time_ms. None = fallback
            to the global value. Lets heavy subagents (e.g. deep_research) run longer than
            the main agent without inflating every agent's budget.
        terminal_tool: Name of the required terminal tool for workflow-finalize guards (B-047).
            None disables the guard (neutral for non-research subagents and the main agent).
        required_before_terminal: Tools that must be called before terminal_tool (B-047).
    """

    id: str
    name: str
    description: str
    capabilities: list[str] = field(default_factory=lambda: [])
    allowed_tools: list[str] = field(default_factory=lambda: ["*"])
    allowed_departments: list[str] = field(default_factory=lambda: ["*"])
    prompt_path: str = ""
    direct_response: bool = False
    max_wall_time_ms: int | None = None
    terminal_tool: str | None = None
    required_before_terminal: list[str] = field(default_factory=lambda: [])
