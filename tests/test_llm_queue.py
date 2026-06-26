"""Tests for LLMRequestQueue — concurrency limits, position tracking, rolling average."""

from __future__ import annotations

import asyncio

import pytest

from corpclaw_lite.llm.queue import LLMQueueStatus, LLMRequestQueue, SlotAffinityConfig


class TestQueueBasic:
    """Basic acquire/release cycle."""

    @pytest.mark.asyncio
    async def test_acquire_release_single(self) -> None:
        q = LLMRequestQueue(max_concurrent=2)
        entry = await q.acquire("user1")
        assert entry.user_id == "user1"
        assert q.active_count == 1
        assert q.queue_length == 0
        await q.release(entry, 5.0)
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

        entry_u1 = await q.acquire("u1")

        result: list[str] = []

        async def waiter() -> None:
            await q.acquire("u2")
            result.append("acquired")

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert result == [], "Should still be waiting"
        assert q.queue_length == 1

        await q.release(entry_u1, 1.0)
        await task
        assert result == ["acquired"]
        assert q.queue_length == 0

    @pytest.mark.asyncio
    async def test_status_callback_emits_initial_and_periodic_waiting_status(self) -> None:
        q = LLMRequestQueue(max_concurrent=1)
        holder = await q.acquire("holder")
        statuses: list[LLMQueueStatus] = []

        waiter = asyncio.create_task(
            q.acquire(
                "waiting-user",
                on_status=statuses.append,
                notify_interval_seconds=0.01,
            )
        )
        await asyncio.sleep(0.035)

        assert len(statuses) >= 2
        assert statuses[0].user_id == "waiting-user"
        assert statuses[0].position == 0
        assert statuses[0].estimated_wait_seconds == 15.0
        assert statuses[0].waiting_count == 1
        assert statuses[0].active_count == 1

        await q.release(holder, 0.1)
        entry = await waiter
        await q.release(entry, 0.1)

    @pytest.mark.asyncio
    async def test_status_callback_errors_do_not_break_acquire(self) -> None:
        q = LLMRequestQueue(max_concurrent=1)
        holder = await q.acquire("holder")
        called = False

        def broken_callback(_status: LLMQueueStatus) -> None:
            nonlocal called
            called = True
            raise RuntimeError("status channel failed")

        waiter = asyncio.create_task(q.acquire("user1", on_status=broken_callback))
        await asyncio.sleep(0.02)
        await q.release(holder, 0.1)
        entry = await waiter

        assert called is True
        assert entry.user_id == "user1"
        assert q.active_count == 1
        await q.release(entry, 0.1)


class TestPositionTracking:
    """Queue position and estimated wait."""

    @pytest.mark.asyncio
    async def test_position_while_waiting(self) -> None:
        q = LLMRequestQueue(max_concurrent=1)
        entry_u1 = await q.acquire("u1")

        # u2 enters queue
        task = asyncio.create_task(q.acquire("u2"))
        await asyncio.sleep(0.02)

        assert q.get_position("u2") == 0
        est = q.get_estimated_wait("u2")
        assert est is not None
        assert est == 15.0  # Next in line still waits for the active request slot

        # u3 enters queue
        task3 = asyncio.create_task(q.acquire("u3"))
        await asyncio.sleep(0.02)

        assert q.get_position("u2") == 0
        assert q.get_position("u3") == 1

        # Cleanup
        await q.release(entry_u1, 1.0)
        entry_u2 = await task
        await q.release(entry_u2, 1.0)
        entry_u3 = await task3
        await q.release(entry_u3, 1.0)

    @pytest.mark.asyncio
    async def test_position_none_when_not_queued(self) -> None:
        q = LLMRequestQueue(max_concurrent=2)
        assert q.get_position("nobody") is None
        assert q.get_estimated_wait("nobody") is None

    @pytest.mark.asyncio
    async def test_position_none_after_acquired(self) -> None:
        q = LLMRequestQueue(max_concurrent=2)
        entry = await q.acquire("u1")
        # Active, not waiting
        assert q.get_position("u1") is None
        await q.release(entry, 1.0)


class TestRollingAverage:
    """Estimated wait time adapts to observed durations."""

    @pytest.mark.asyncio
    async def test_avg_updates_on_release(self) -> None:
        q = LLMRequestQueue(max_concurrent=2)

        entry_u1 = await q.acquire("u1")
        await q.release(entry_u1, 10.0)
        # Default 15s, weight 0.2: 0.8*15 + 0.2*10 = 14.0
        assert abs(q._avg_request_seconds - 14.0) < 0.01

        entry_u2 = await q.acquire("u2")
        await q.release(entry_u2, 20.0)
        # 0.8*14 + 0.2*20 = 15.2
        assert abs(q._avg_request_seconds - 15.2) < 0.01


class TestWaitingEntries:
    """Snapshot of waiting users for notification loop."""

    @pytest.mark.asyncio
    async def test_get_waiting_entries(self) -> None:
        q = LLMRequestQueue(max_concurrent=1)
        entry_u1 = await q.acquire("u1")

        t2 = asyncio.create_task(q.acquire("u2"))
        t3 = asyncio.create_task(q.acquire("u3"))
        await asyncio.sleep(0.05)

        entries = q.get_waiting_entries()
        ids = {e.user_id for e in entries}
        assert ids == {"u2", "u3"}

        await q.release(entry_u1, 1.0)
        entry_u2 = await t2
        await q.release(entry_u2, 1.0)
        entry_u3 = await t3
        await q.release(entry_u3, 1.0)

    @pytest.mark.asyncio
    async def test_get_entry_by_id(self) -> None:
        q = LLMRequestQueue(max_concurrent=1)
        entry_u1 = await q.acquire("u1")

        t = asyncio.create_task(q.acquire("target"))
        await asyncio.sleep(0.02)

        entry = q.get_entry("target")
        assert entry is not None
        assert entry.user_id == "target"

        no_entry = q.get_entry("nobody")
        assert no_entry is None

        await q.release(entry_u1, 1.0)
        entry_target = await t
        await q.release(entry_target, 1.0)


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
            entry = await q.acquire(uid)
            async with lock:
                current = q.active_count
                if current > peak_active:
                    peak_active = current
            await asyncio.sleep(0.05)
            await q.release(entry, 0.05)

        await asyncio.gather(*[worker(f"u{i}") for i in range(10)])
        assert peak_active <= max_c
        assert q.active_count == 0
        assert q.queue_length == 0


class TestSlotAffinity:
    """llama.cpp slot-affinity scheduling."""

    @pytest.mark.asyncio
    async def test_interactive_users_get_sticky_slots_then_overflow(self) -> None:
        q = LLMRequestQueue(
            max_concurrent=4,
            strategy="slot_affinity",
            slot_affinity=SlotAffinityConfig(
                enabled=True,
                provider_names=("llamacpp",),
                sticky_slot_ids=(0, 1, 2),
                overflow_slot_ids=(3,),
            ),
        )

        e1 = await q.acquire("u1", provider_name="llamacpp")
        e2 = await q.acquire("u2", provider_name="llamacpp")
        e3 = await q.acquire("u3", provider_name="llamacpp")
        e4 = await q.acquire("u4", provider_name="llamacpp")

        assert (e1.slot_id, e1.slot_kind) == (0, "sticky")
        assert (e2.slot_id, e2.slot_kind) == (1, "sticky")
        assert (e3.slot_id, e3.slot_kind) == (2, "sticky")
        assert (e4.slot_id, e4.slot_kind) == (3, "overflow")
        assert e1.backend_extra_body == {"id_slot": 0, "cache_prompt": True}
        assert e4.backend_extra_body == {"id_slot": 3, "cache_prompt": True}

        await q.release(e4, 1.0)
        await q.release(e3, 1.0)
        await q.release(e2, 1.0)
        await q.release(e1, 1.0)

    @pytest.mark.asyncio
    async def test_sticky_slot_reused_before_ttl(self) -> None:
        q = LLMRequestQueue(
            max_concurrent=4,
            strategy="slot_affinity",
            slot_affinity=SlotAffinityConfig(
                enabled=True,
                provider_names=("llamacpp",),
                sticky_slot_ids=(0, 1, 2),
                overflow_slot_ids=(3,),
                idle_ttl_seconds=120.0,
            ),
        )

        first = await q.acquire("u1", provider_name="llamacpp")
        await q.release(first, 1.0)
        second = await q.acquire("u1", provider_name="llamacpp")

        assert second.slot_id == first.slot_id
        assert second.slot_kind == "sticky"
        assert second.assignment_reused is True

        await q.release(second, 1.0)

    @pytest.mark.asyncio
    async def test_auxiliary_load_uses_overflow(self) -> None:
        q = LLMRequestQueue(
            max_concurrent=4,
            strategy="slot_affinity",
            slot_affinity=SlotAffinityConfig(
                enabled=True,
                provider_names=("llamacpp",),
                sticky_slot_ids=(0, 1, 2),
                overflow_slot_ids=(3,),
            ),
        )

        entry = await q.acquire(
            "u1",
            task_kind="vision",
            load_class="vision",
            provider_name="llamacpp",
        )

        assert (entry.slot_id, entry.slot_kind) == (3, "overflow")
        await q.release(entry, 1.0)

    @pytest.mark.asyncio
    async def test_non_matching_provider_uses_simple_queue(self) -> None:
        q = LLMRequestQueue(
            max_concurrent=4,
            strategy="slot_affinity",
            slot_affinity=SlotAffinityConfig(
                enabled=True,
                provider_names=("llamacpp",),
                sticky_slot_ids=(0, 1, 2),
                overflow_slot_ids=(3,),
            ),
        )

        entry = await q.acquire("u1", provider_name="litellm")

        assert entry.slot_id is None
        assert entry.slot_kind == "simple"
        assert entry.backend_extra_body == {}
        await q.release(entry, 1.0)
