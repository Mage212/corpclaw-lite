from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RiskLevel(StrEnum):
    """Risk levels for tools to determine pre-approval needs."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolParam(BaseModel):
    """Parameter definition for a tool."""

    name: str = Field(..., description="Name of the parameter")
    type: str = Field(..., description="Type of the parameter (e.g., string, boolean)")
    description: str = Field(..., description="Description of the parameter")
    required: bool = Field(True, description="Whether the parameter is required")
    enum: list[str] | None = Field(None, description="Allowed values if restricted")


class Tool(ABC):
    """Base class for all executable tools in CorpClaw Lite."""

    name: str
    description: str
    params: list[ToolParam]
    risk_level: RiskLevel = RiskLevel.LOW

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """Execute the tool with the given arguments."""
        ...
