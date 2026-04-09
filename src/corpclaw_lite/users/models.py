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
    department: str = Field(..., description="Department slug used for RBAC")
    name: str = Field(..., description="Full Name or Telegram Username")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Creation time"
    )

    def memory_key(self) -> str:
        """Return the key used for memory storage.

        Prefer telegram_id for consistency with onboarding which stores
        facts under telegram_id.  Falls back to internal DB id for CLI users.
        """
        return str(self.telegram_id) if self.telegram_id else str(self.id)
