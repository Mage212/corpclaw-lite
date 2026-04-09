from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Skill",
]


@dataclass(frozen=True)
class Skill:
    """Core skill representation - pure data, no logic.

    Attributes:
        id: Unique identifier
        description: Short summary of what this skill does
        allowed_for: List of department slugs allowed to use this, or ["*"] for all
        instructions: The full markdown instructions for the agent
        path: Optional file path where this skill was loaded from
        version: Skill version string
        keywords: Matching terms/prefixes for semantic selection (e.g. ["excel", "нормализ"])
        always: If True, this skill is always injected into the prompt regardless of matching
    """

    id: str
    description: str
    allowed_for: list[str]
    instructions: str
    path: Path | None = None
    version: str = "1.0.0"
    keywords: list[str] = field(default_factory=lambda: [])
    always: bool = False
