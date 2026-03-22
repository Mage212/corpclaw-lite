from dataclasses import dataclass
from pathlib import Path


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
    """
    id: str
    description: str
    allowed_for: list[str]
    instructions: str
    path: Path | None = None
    version: str = "1.0.0"
