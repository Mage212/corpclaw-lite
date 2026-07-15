"""B-118: headless prompt builder for scheduled agent runs."""

from __future__ import annotations

from datetime import datetime

__all__ = ["build_headless_prompt"]


def build_headless_prompt(
    *,
    task_text: str,
    schedule_text: str,
    now_local: datetime,
    tz: str,
) -> str:
    """Assemble the synthetic user message for a scheduled headless run."""
    body = task_text.strip()
    when = now_local.isoformat(timespec="minutes")
    return (
        f"{body}\n\n"
        f"[Контекст запуска по расписанию]\n"
        f"Сейчас: {when} ({tz})\n"
        f"Формулировка расписания: {schedule_text.strip()}\n"
        f"Выполни задание выше. Не создавай новые задачи по расписанию.\n"
    )
