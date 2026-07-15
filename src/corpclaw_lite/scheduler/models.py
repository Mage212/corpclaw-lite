"""B-118 / DC-030: scheduled task models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "ScheduleKind",
    "ScheduleSpec",
    "ScheduledTask",
    "TaskStatus",
]

TaskStatus = Literal["pending", "active", "paused", "done", "dismissed"]
ScheduleKind = Literal["once", "interval", "cron", "unset"]


@dataclass(slots=True)
class ScheduleSpec:
    """Machine-readable schedule (poll uses this, not free-form text)."""

    kind: ScheduleKind
    # once: ISO datetime string (aware or naive→tz applied)
    run_at: str | None = None
    # interval: period in whole minutes (>= 1)
    minutes: int | None = None
    # cron: 5-field expression
    expr: str | None = None

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"kind": self.kind}
        if self.run_at is not None:
            body["run_at"] = self.run_at
        if self.minutes is not None:
            body["minutes"] = self.minutes
        if self.expr is not None:
            body["expr"] = self.expr
        return body

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> ScheduleSpec:
        if not data:
            return cls(kind="unset")
        kind_raw = str(data.get("kind") or "unset")
        kind: ScheduleKind = (
            kind_raw  # type: ignore[assignment]
            if kind_raw in {"once", "interval", "cron", "unset"}
            else "unset"
        )
        minutes = data.get("minutes")
        return cls(
            kind=kind,
            run_at=str(data["run_at"]) if data.get("run_at") is not None else None,
            minutes=int(minutes) if minutes is not None else None,
            expr=str(data["expr"]) if data.get("expr") is not None else None,
        )


@dataclass(slots=True)
class ScheduledTask:
    """One user-owned scheduled agent task."""

    id: str
    user_id: int
    title: str
    task_text: str
    schedule_text: str
    schedule: ScheduleSpec
    timezone: str
    status: TaskStatus
    enabled: bool
    dedup_key: str
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    run_count: int = 0
    error_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    accepted_at: str | None = None
    extra: dict[str, Any] = field(default_factory=lambda: {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "task_text": self.task_text,
            "schedule_text": self.schedule_text,
            "schedule": self.schedule.to_json(),
            "timezone": self.timezone,
            "status": self.status,
            "enabled": self.enabled,
            "dedup_key": self.dedup_key,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "accepted_at": self.accepted_at,
        }
