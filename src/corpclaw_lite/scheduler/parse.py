"""B-118: deterministic schedule parsing + next_run computation."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from corpclaw_lite.scheduler.models import ScheduleSpec

__all__ = [
    "ScheduleParseError",
    "compute_next_run",
    "parse_schedule",
    "resolve_zone",
]

_DURATION_RE = re.compile(
    r"^(?:(?P<days>\d+)\s*d(?:ays?)?)?\s*"
    r"(?:(?P<hours>\d+)\s*h(?:ours?)?)?\s*"
    r"(?:(?P<minutes>\d+)\s*m(?:in(?:utes?)?)?)?\s*$",
    re.IGNORECASE,
)
_EVERY_RE = re.compile(r"^every\s+(.+)$", re.IGNORECASE)
_BARE_DURATION_RE = re.compile(r"^(\d+)\s*([dhm])$", re.IGNORECASE)


class ScheduleParseError(ValueError):
    """Raised when a schedule string cannot be parsed deterministically."""


def resolve_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleParseError(f"Unknown timezone '{name}'") from exc


def _duration_to_minutes(text: str) -> int:
    text = text.strip()
    bare = _BARE_DURATION_RE.match(text)
    if bare:
        n = int(bare.group(1))
        unit = bare.group(2).lower()
        if unit == "d":
            return n * 24 * 60
        if unit == "h":
            return n * 60
        return n
    m = _DURATION_RE.match(text)
    if not m:
        raise ScheduleParseError(f"Invalid duration '{text}'")
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    total = days * 24 * 60 + hours * 60 + minutes
    if total <= 0:
        raise ScheduleParseError(f"Duration must be positive: '{text}'")
    return total


def parse_schedule(schedule: str, *, now: datetime | None = None, tz: str = "UTC") -> ScheduleSpec:
    """Parse human/ops schedule strings into :class:`ScheduleSpec`.

    Supported:
    - ``30m`` / ``2h`` / ``1d`` — one-shot after duration from *now*
    - ``every 30m`` / ``every 2h`` — recurring interval
    - ``0 9 * * 1-5`` — cron (5 fields)
    - ISO timestamp — one-shot absolute
    """
    raw = (schedule or "").strip()
    if not raw:
        raise ScheduleParseError("Empty schedule")

    zone = resolve_zone(tz)
    now_local = (now or datetime.now(UTC)).astimezone(zone)

    every = _EVERY_RE.match(raw)
    if every:
        minutes = _duration_to_minutes(every.group(1))
        return ScheduleSpec(kind="interval", minutes=minutes)

    # Bare duration → once after delay
    try:
        minutes = _duration_to_minutes(raw)
        run_at = (now_local + timedelta(minutes=minutes)).isoformat()
        return ScheduleSpec(kind="once", run_at=run_at)
    except ScheduleParseError:
        pass

    # Cron (5 whitespace-separated fields)
    parts = raw.split()
    if len(parts) == 5:
        try:
            from croniter import croniter

            croniter(raw, now_local)
        except Exception as exc:
            raise ScheduleParseError(f"Invalid cron expression '{raw}': {exc}") from exc
        return ScheduleSpec(kind="cron", expr=raw)

    # ISO timestamp
    if "T" in raw or re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        try:
            cleaned = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=zone)
            return ScheduleSpec(kind="once", run_at=dt.isoformat())
        except ValueError as exc:
            raise ScheduleParseError(f"Invalid timestamp '{raw}': {exc}") from exc

    raise ScheduleParseError(
        f"Invalid schedule '{raw}'. Use: 30m | every 2h | 0 9 * * * | ISO datetime"
    )


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def compute_next_run(
    spec: ScheduleSpec | dict[str, Any],
    *,
    last_run_at: datetime | None = None,
    now: datetime | None = None,
    tz: str = "UTC",
) -> datetime | None:
    """Return next fire time in UTC, or None if schedule is exhausted/unset."""
    schedule = ScheduleSpec.from_json(spec) if isinstance(spec, dict) else spec

    zone = resolve_zone(tz)
    now_utc = _as_utc(now or datetime.now(UTC))
    now_local = now_utc.astimezone(zone)
    base_local = _as_utc(last_run_at).astimezone(zone) if last_run_at is not None else now_local

    if schedule.kind == "unset":
        return None

    if schedule.kind == "once":
        if not schedule.run_at:
            return None
        run_at = datetime.fromisoformat(schedule.run_at.replace("Z", "+00:00"))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=zone)
        run_utc = _as_utc(run_at)
        # One-shot already fired
        if last_run_at is not None and _as_utc(last_run_at) >= run_utc:
            return None
        return run_utc

    if schedule.kind == "interval":
        minutes = int(schedule.minutes or 0)
        if minutes <= 0:
            return None
        if last_run_at is None:
            # First run: treat like "every N from now" → next is now (due immediately
            # after accept) only if we want; product: first fire at accept+interval
            # is surprising. Fire first at now (due on next tick after accept).
            return now_utc
        return _as_utc(last_run_at) + timedelta(minutes=minutes)

    if schedule.kind == "cron":
        if not schedule.expr:
            return None
        from croniter import croniter

        # After a run, advance from last_run; else from now
        start = base_local if last_run_at is not None else now_local
        itr = croniter(schedule.expr, start)
        nxt = itr.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=zone)
        # If we started from last_run and next is still in the past, keep going
        while _as_utc(nxt) <= now_utc and last_run_at is not None:
            nxt = itr.get_next(datetime)
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=zone)
            # safety
            if _as_utc(nxt) > now_utc + timedelta(days=400):
                break
        if last_run_at is None and _as_utc(nxt) < now_utc:
            nxt = itr.get_next(datetime)
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=zone)
        return _as_utc(nxt)

    return None
