"""B-118: schedule parse + next_run."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from corpclaw_lite.scheduler.parse import (
    ScheduleParseError,
    compute_next_run,
    parse_schedule,
)


def test_parse_duration_once() -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    spec = parse_schedule("30m", now=now, tz="UTC")
    assert spec.kind == "once"
    assert spec.run_at is not None
    assert "12:30" in spec.run_at or "12:30:00" in spec.run_at


def test_parse_every_interval() -> None:
    spec = parse_schedule("every 2h", tz="UTC")
    assert spec.kind == "interval"
    assert spec.minutes == 120


def test_parse_cron() -> None:
    spec = parse_schedule("0 9 * * 1-5", tz="Europe/Moscow")
    assert spec.kind == "cron"
    assert spec.expr == "0 9 * * 1-5"


def test_parse_iso_once() -> None:
    spec = parse_schedule("2026-08-01T10:00:00+00:00", tz="UTC")
    assert spec.kind == "once"
    assert spec.run_at is not None


def test_parse_invalid() -> None:
    with pytest.raises(ScheduleParseError):
        parse_schedule("next coffee break", tz="UTC")


def test_compute_next_interval() -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    from corpclaw_lite.scheduler.models import ScheduleSpec

    spec = ScheduleSpec(kind="interval", minutes=60)
    first = compute_next_run(spec, last_run_at=None, now=now, tz="UTC")
    assert first is not None
    # H4: first fire is now + period, not immediately due
    assert (first - now).total_seconds() == 3600
    second = compute_next_run(spec, last_run_at=now, now=now, tz="UTC")
    assert second is not None
    assert (second - now).total_seconds() == 3600


def test_compute_next_once_exhausted() -> None:
    from corpclaw_lite.scheduler.models import ScheduleSpec

    run_at = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    spec = ScheduleSpec(kind="once", run_at=run_at.isoformat())
    fired = compute_next_run(
        spec,
        last_run_at=run_at,
        now=datetime(2026, 7, 15, 11, 0, tzinfo=UTC),
        tz="UTC",
    )
    assert fired is None
