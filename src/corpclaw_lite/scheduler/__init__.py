"""B-118 / DC-030: scheduler backbone (consent-first agent-on-schedule)."""

from __future__ import annotations

from corpclaw_lite.scheduler.models import ScheduledTask, ScheduleSpec
from corpclaw_lite.scheduler.parse import ScheduleParseError, compute_next_run, parse_schedule
from corpclaw_lite.scheduler.prompt import build_headless_prompt
from corpclaw_lite.scheduler.service import SchedulerError, SchedulerService, make_dedup_key
from corpclaw_lite.scheduler.store import SchedulerStore

__all__ = [
    "ScheduleParseError",
    "ScheduleSpec",
    "ScheduledTask",
    "SchedulerError",
    "SchedulerService",
    "SchedulerStore",
    "build_headless_prompt",
    "compute_next_run",
    "make_dedup_key",
    "parse_schedule",
]
