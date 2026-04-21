"""Tests for LLMRequestQueue — concurrency limits, position tracking, rolling average."""

from __future__ import annotations

import asyncio
import time

import pytest

from corpclaw_lite.llm.queue import LLMRequestQueue


class TestQueueBasic:
    """Basic acquire/release cycle."""

    @pytest.mark.asyncio
    async def test_acquire_release_single(self) -> None:
        q = LLMRequestQueue(max_concurrent=2)
        entry = await q.acquire("user1")
        assert entry.user_id == "user1"
        assert q.active_count == 1
        assert q.queue_length == 0
        await q.release("user1", 5.0)
        assert q.active_count == 0

    @pytest.mark.asyncio
    async def test_acquire_fills_to_capacity(self) -> None:
        q = LLMRequestQueue(max_concurrent=2)
        await q.acquire("u1")
        await q.acquire("u2")
        assert q.active_count == 2

    @pytest.mark.asyncio
    async def test_blocking_acquire(self) -> None:
        """Third request blocks until a slot is released."""
        q = LLMRequestQueue(max_concurrent=1)

        await q.acquire("u1")

        result: list[str] = []

        async def waiter() -> None:
            await q.acquire("u2")
            result.append("acquired")

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert result == [], "Should still be waiting"
        assert q.queue_length == 1

        await q.release("u1", 1.0)
        await task
        assert result == ["acquired"]
        assert q.queue_length == 0


class TestPositionTracking:
    """Queue position and estimated wait."""

    @pytest.mark.asyncio
    async def test_position_while_waiting(self) -> None:
        q = LLMRequestQueue(max_concurrent=1)
        await q.acquire("u1")

        # u2 enters queue
        task = asyncio.create_task(q.acquire("u2"))
        await asyncio.sleep(0.02)

        assert q.get_position("u2") == 0
        est = q.get_estimated_wait("u2")
        assert est is not None
        assert est == 0.0  # Position 0 = next in line, no one ahead

        # u3 enters queue
        task3 = asyncio.create_task(q.acquire("u3"))
        await asyncio.sleep(0.02)

        assert q.get_position("u2") == 0
        assert q.get_position("u3") == 1

        # Cleanup
        await q.release("u1", 1.0)
        await task
        await q.release("u2", 1.0)
        await task3
        await q.release("u3", 1.0)

    @pytest.mark.asyncio
    async def test_position_none_when_not_queued(self) -> None:
        q = LLMRequestQueue(max_concurrent=2)
        assert q.get_position("nobody") is None
        assert q.get_estimated_wait("nobody") is None

    @pytest.mark.asyncio
    async def test_position_none_after_acquired(self) -> None:
        q = LLMRequestQueue(max_concurrent=2)
        await q.acquire("u1")
        # Active, not waiting
        assert q.get_position("u1") is None
        await q.release("u1", 1.0)


class TestRollingAverage:
    """Estimated wait time adapts to observed durations."""

    @pytest.mark.asyncio
    async def test_avg_updates_on_release(self) -> None:
        q = LLMRequestQueue(max_concurrent=2)

        await q.acquire("u1")
        await q.release("u1", 10.0)
        # Default 15s, weight 0.2: 0.8*15 + 0.2*10 = 14.0
        assert abs(q._avg_request_seconds - 14.0) < 0.01

        await q.acquire("u2")
        await q.release("u2", 20.0)
        # 0.8*14 + 0.2*20 = 15.2
        assert abs(q._avg_request_seconds - 15.2) < 0.01


class TestWaitingEntries:
    """Snapshot of waiting users for notification loop."""

    @pytest.mark.asyncio
    async def test_get_waiting_entries(self) -> None:
        q = LLMRequestQueue(max_concurrent=1)
        await q.acquire("u1")

        t2 = asyncio.create_task(q.acquire("u2"))
        t3 = asyncio.create_task(q.acquire("u3"))
        await asyncio.sleep(0.05)

        entries = q.get_waiting_entries()
        ids = {e.user_id for e in entries}
        assert ids == {"u2", "u3"}

        await q.release("u1", 1.0)
        await t2
        await q.release("u2", 1.0)
        await t3
        await q.release("u3", 1.0)

    @pytest.mark.asyncio
    async def test_get_entry_by_id(self) -> None:
        q = LLMRequestQueue(max_concurrent=1)
        await q.acquire("u1")

        t = asyncio.create_task(q.acquire("target"))
        await asyncio.sleep(0.02)

        entry = q.get_entry("target")
        assert entry is not None
        assert entry.user_id == "target"

        no_entry = q.get_entry("nobody")
        assert no_entry is None

        await q.release("u1", 1.0)
        await t
        await q.release("target", 1.0)


class TestConcurrentAccess:
    """Multiple concurrent acquirers don't exceed the limit."""

    @pytest.mark.asyncio
    async def test_never_exceeds_max_concurrent(self) -> None:
        max_c = 3
        q = LLMRequestQueue(max_concurrent=max_c)
        peak_active = 0
        lock = asyncio.Lock()

        async def worker(uid: str) -> None:
            nonlocal peak_active
            await q.acquire(uid)
            async with lock:
                current = q.active_count
                if current > peak_active:
                    peak_active = current
            await asyncio.sleep(0.05)
            await q.release(uid, 0.05)

        await asyncio.gather(*[worker(f"u{i}") for i in range(10)])
        assert peak_active <= max_c
        assert q.active_count == 0
        assert q.queue_length == 0
