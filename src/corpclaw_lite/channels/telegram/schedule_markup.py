"""B-143 PR2: InlineKeyboard for schedule_propose consent."""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from corpclaw_lite.channels.telegram.callback_data import (
    schedule_accept_data,
    schedule_dismiss_data,
)

__all__ = [
    "build_schedule_confirm_markup",
    "markup_from_notify_metadata",
]


def build_schedule_confirm_markup(task_id: str) -> InlineKeyboardMarkup:
    """Accept / Dismiss buttons for a pending scheduled task."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Подтвердить",
                    callback_data=schedule_accept_data(task_id),
                ),
                InlineKeyboardButton(
                    "❌ Отклонить",
                    callback_data=schedule_dismiss_data(task_id),
                ),
            ]
        ]
    )


def markup_from_notify_metadata(metadata: dict[str, object]) -> InlineKeyboardMarkup | None:
    """Build keyboard when proactive metadata is a schedule_confirm card."""
    if metadata.get("kind") != "schedule_confirm":
        return None
    task_id = metadata.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    status = metadata.get("status")
    if status is not None and status != "pending":
        return None
    return build_schedule_confirm_markup(task_id.strip())


def markup_from_notify_metadata_any(metadata: Any) -> InlineKeyboardMarkup | None:
    if not isinstance(metadata, dict):
        return None
    return markup_from_notify_metadata(metadata)  # type: ignore[arg-type]
