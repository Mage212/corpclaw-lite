from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class RiskLevel(StrEnum):
    """Risk levels for tools to determine pre-approval needs."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolParam(BaseModel):
    """Parameter definition for a tool."""

    name: str
    type: str
    description: str
    required: bool = True
    enum: list[str] | None = None


class Tool(ABC):
    """Base class for all executable tools in CorpClaw Lite."""

    name: str
    description: str
    params: list[ToolParam]
    risk_level: RiskLevel = RiskLevel.LOW
    parallel_safe: bool = True

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """Execute the tool with the given arguments."""
        ...
