from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

__all__ = [
    "User",
]


class User(BaseModel):
    """User representation in the system."""

    id: int = Field(..., description="Internal User ID")
    telegram_id: int | None = Field(None, description="Telegram User ID if registered via TG")
    username: str | None = None
    department: str = Field(..., description="Department slug used for RBAC")
    name: str = Field(..., description="Full Name or Telegram Username")
    is_admin: bool = False
    disabled: bool = False
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Creation time"
    )

    def memory_key(self) -> str:
        """Return the canonical key used for memory storage."""
        return str(self.id)

    def workspace_key(self) -> str:
        """Return stable per-user workspace suffix."""
        return str(self.id)
