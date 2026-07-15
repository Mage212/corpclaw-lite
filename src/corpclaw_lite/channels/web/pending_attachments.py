"""In-memory pending inline attachments per (user, session) — B-094."""

from __future__ import annotations

from corpclaw_lite.agent.inline_attach import InlineAttachment

__all__ = [
    "MAX_PENDING_ATTACHMENTS",
    "PendingAttachmentsStore",
]

MAX_PENDING_ATTACHMENTS = 5


class PendingAttachmentsStore:
    """Process-local pending attach queue (lost on restart — re-attach is cheap)."""

    def __init__(self, *, max_pending: int = MAX_PENDING_ATTACHMENTS) -> None:
        if max_pending < 1:
            msg = "max_pending must be >= 1"
            raise ValueError(msg)
        self._max = max_pending
        self._items: dict[tuple[int, int], list[InlineAttachment]] = {}

    def list(self, user_id: int, session_id: int) -> list[InlineAttachment]:
        return list(self._items.get((user_id, session_id), []))

    def count(self, user_id: int, session_id: int) -> int:
        return len(self._items.get((user_id, session_id), []))

    def add(self, user_id: int, session_id: int, item: InlineAttachment) -> list[InlineAttachment]:
        key = (user_id, session_id)
        current = list(self._items.get(key, []))
        # Replace same path if re-attached.
        current = [x for x in current if x.path != item.path]
        if len(current) >= self._max:
            msg = f"Too many pending attachments (max {self._max})"
            raise ValueError(msg)
        current.append(item)
        self._items[key] = current
        return list(current)

    def remove(self, user_id: int, session_id: int, path: str) -> list[InlineAttachment]:
        key = (user_id, session_id)
        current = [x for x in self._items.get(key, []) if x.path != path]
        if current:
            self._items[key] = current
        else:
            self._items.pop(key, None)
        return list(current)

    def clear(self, user_id: int, session_id: int) -> list[InlineAttachment]:
        return self._items.pop((user_id, session_id), [])

    def pop_all(self, user_id: int, session_id: int) -> list[InlineAttachment]:
        """Return and clear pending list for compose-on-send."""
        return self._items.pop((user_id, session_id), [])

    def clear_user(self, user_id: int) -> None:
        dead = [key for key in self._items if key[0] == user_id]
        for key in dead:
            del self._items[key]
