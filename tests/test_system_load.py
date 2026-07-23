"""DC-008 / D-088: system_load payload helpers (counts only, no PII)."""

from __future__ import annotations

from corpclaw_lite.channels.status import (
    SYSTEM_LOAD_PAYLOAD_KEYS,
    build_system_load_payload,
    compute_load_level,
)


def test_compute_load_level_idle() -> None:
    assert compute_load_level(active_count=0, max_concurrent=4, waiting_count=0) == "idle"
    assert compute_load_level(active_count=2, max_concurrent=4, waiting_count=0) == "idle"


def test_compute_load_level_zero_capacity() -> None:
    """Misconfigured max_concurrent=0 must not look saturated when empty."""
    assert compute_load_level(active_count=0, max_concurrent=0, waiting_count=0) == "idle"
    assert compute_load_level(active_count=0, max_concurrent=0, waiting_count=1) == "busy"


def test_compute_load_level_busy() -> None:
    # Full capacity but nobody waiting yet
    assert compute_load_level(active_count=4, max_concurrent=4, waiting_count=0) == "busy"
    # Waiters while capacity remains (e.g. sticky slot contention)
    assert compute_load_level(active_count=2, max_concurrent=4, waiting_count=1) == "busy"


def test_compute_load_level_saturated() -> None:
    assert compute_load_level(active_count=4, max_concurrent=4, waiting_count=1) == "saturated"
    assert compute_load_level(active_count=4, max_concurrent=4, waiting_count=3) == "saturated"


def test_build_system_load_payload_shape_and_allowlist() -> None:
    payload = build_system_load_payload(
        active_count=2,
        max_concurrent=4,
        waiting_count=1,
        active_users=3,
        updated_at=1_700_000_000_000,
    )
    assert payload["type"] == "system_load"
    assert payload["active_count"] == 2
    assert payload["max_concurrent"] == 4
    assert payload["waiting_count"] == 1
    assert payload["active_users"] == 3
    assert payload["load_level"] == "busy"
    assert payload["updated_at"] == 1_700_000_000_000
    assert set(payload.keys()) == SYSTEM_LOAD_PAYLOAD_KEYS


def test_build_system_load_payload_no_pii_keys() -> None:
    payload = build_system_load_payload(
        active_count=0,
        max_concurrent=4,
        waiting_count=0,
        active_users=0,
    )
    forbidden = {
        "user_id",
        "session_id",
        "name",
        "title",
        "my_queue_position",
        "position",
        "telegram_id",
    }
    assert forbidden.isdisjoint(payload.keys())
    assert payload["load_level"] == "idle"
