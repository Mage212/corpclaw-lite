from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from corpclaw_lite.llm.base import LLMResponse, TokenUsage
from corpclaw_lite.llm.cache import (
    LLMCacheManager,
    LLMCacheMetadata,
    LLMCacheScope,
    PersistentCacheConfig,
    SlotCacheActionResult,
)
from corpclaw_lite.llm.queue import LLMRequestQueue, SlotAffinityConfig


class FakeSlotCacheClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    async def save(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        self.calls.append(("save", slot_id, filename))
        return SlotCacheActionResult(
            ok=True,
            status_code=200,
            action="save",
            slot_id=slot_id,
            filename=filename,
            n_tokens=1200,
            n_bytes=56_000_000,
            server_ms=12.0,
        )

    async def restore(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        self.calls.append(("restore", slot_id, filename))
        return SlotCacheActionResult(
            ok=True,
            status_code=200,
            action="restore",
            slot_id=slot_id,
            filename=filename,
            n_tokens=1200,
            n_bytes=56_000_000,
            server_ms=10.0,
        )

    async def erase(self, slot_id: int, *, model: str) -> SlotCacheActionResult:
        self.calls.append(("erase", slot_id, None))
        return SlotCacheActionResult(
            ok=True,
            status_code=200,
            action="erase",
            slot_id=slot_id,
            n_tokens=1200,
            server_ms=2.0,
        )


def _config(tmp_path: Path) -> PersistentCacheConfig:
    return PersistentCacheConfig(
        enabled=True,
        root_dir=tmp_path / "slot-cache",
        index_path=tmp_path / "index.sqlite",
        save_min_tokens=1,
        save_dirty_seconds=0,
        validation_min_reuse_ratio=0.70,
    )


def _queue() -> LLMRequestQueue:
    return LLMRequestQueue(
        max_concurrent=1,
        strategy="slot_affinity",
        slot_affinity=SlotAffinityConfig(
            enabled=True,
            provider_names=("llamacpp",),
            sticky_slot_ids=(0,),
            overflow_slot_ids=(),
        ),
    )


def _response(*, cached: int = 0, prompt: int = 1000, prompt_n: int = 1000) -> LLMResponse:
    return LLMResponse(
        content="ok",
        usage=TokenUsage(
            input_tokens=prompt,
            output_tokens=10,
            cached_input_tokens=cached,
            prompt_processing_tokens=prompt_n,
        ),
    )


@pytest.mark.asyncio
async def test_cache_scope_is_agent_specific(tmp_path: Path) -> None:
    manager = LLMCacheManager(
        _config(tmp_path),
        provider_base_urls={"llamacpp": "http://llama:8080/v1"},
        client=FakeSlotCacheClient(),
    )

    main_scope = manager.build_scope(
        user_id="u1",
        conversation_id="default",
        agent_id="main",
        provider_name="llamacpp",
        model="gpt-oss",
        preset="default",
        system="system",
        tools=[],
    )
    subagent_scope = manager.build_scope(
        user_id="u1",
        conversation_id="default",
        agent_id="data-agent",
        provider_name="llamacpp",
        model="gpt-oss",
        preset="default",
        system="system",
        tools=[],
    )

    assert main_scope.key != subagent_scope.key
    assert main_scope.filename != subagent_scope.filename


@pytest.mark.asyncio
async def test_cold_request_saves_cache_metadata(tmp_path: Path) -> None:
    client = FakeSlotCacheClient()
    manager = LLMCacheManager(
        _config(tmp_path),
        provider_base_urls={"llamacpp": "http://llama:8080/v1"},
        client=client,
    )
    queue = _queue()
    entry = await queue.acquire("u1", provider_name="llamacpp")
    scope = manager.build_scope(
        user_id="u1",
        conversation_id="default",
        agent_id="main",
        provider_name="llamacpp",
        model="gpt-oss",
        preset=None,
        system="system",
        tools=[],
    )

    lease = await manager.prepare(entry, scope)
    result = await manager.finalize(entry, lease, _response())

    assert result.retry_without_cache is False
    assert ("save", 0, scope.filename) in client.calls
    await queue.release(entry, 1.0)


@pytest.mark.asyncio
async def test_l2_restore_then_low_reuse_requests_retry(tmp_path: Path) -> None:
    client = FakeSlotCacheClient()
    config = _config(tmp_path)
    first_manager = LLMCacheManager(
        config,
        provider_base_urls={"llamacpp": "http://llama:8080/v1"},
        client=client,
    )
    first_queue = _queue()
    first_entry = await first_queue.acquire("u1", provider_name="llamacpp")
    scope = first_manager.build_scope(
        user_id="u1",
        conversation_id="default",
        agent_id="main",
        provider_name="llamacpp",
        model="gpt-oss",
        preset=None,
        system="system",
        tools=[],
    )
    first_lease = await first_manager.prepare(first_entry, scope)
    await first_manager.finalize(first_entry, first_lease, _response())
    await first_queue.release(first_entry, 1.0)

    second_manager = LLMCacheManager(
        config,
        provider_base_urls={"llamacpp": "http://llama:8080/v1"},
        client=client,
    )
    second_queue = _queue()
    second_entry = await second_queue.acquire("u1", provider_name="llamacpp")
    second_lease = await second_manager.prepare(second_entry, scope)
    result = await second_manager.finalize(
        second_entry,
        second_lease,
        _response(cached=100, prompt=1000, prompt_n=900),
    )

    assert second_lease.hit_kind == "l2"
    assert ("restore", 0, scope.filename) in client.calls
    assert result.retry_without_cache is True
    assert result.mismatch_reason == "low_cache_reuse_ratio"
    assert ("erase", 0, None) in client.calls
    await second_queue.release(second_entry, 1.0)


@pytest.mark.asyncio
async def test_user_reset_skips_l2_restore_and_erases_slot(tmp_path: Path) -> None:
    client = FakeSlotCacheClient()
    config = _config(tmp_path)
    first_manager = LLMCacheManager(
        config,
        provider_base_urls={"llamacpp": "http://llama:8080/v1"},
        client=client,
    )
    first_queue = _queue()
    first_entry = await first_queue.acquire("u1", provider_name="llamacpp")
    scope = first_manager.build_scope(
        user_id="u1",
        conversation_id="default",
        agent_id="main",
        provider_name="llamacpp",
        model="gpt-oss",
        preset=None,
        system="system",
        tools=[],
    )
    first_lease = await first_manager.prepare(first_entry, scope)
    await first_manager.finalize(first_entry, first_lease, _response())
    await first_queue.release(first_entry, 1.0)

    second_manager = LLMCacheManager(
        config,
        provider_base_urls={"llamacpp": "http://llama:8080/v1"},
        client=client,
    )
    await second_manager.mark_user_reset("u1")
    second_queue = _queue()
    second_entry = await second_queue.acquire("u1", provider_name="llamacpp")
    second_lease = await second_manager.prepare(second_entry, scope)

    assert second_lease.hit_kind == "none"
    assert ("erase", 0, None) in client.calls
    assert ("restore", 0, scope.filename) not in client.calls
    await second_queue.release(second_entry, 1.0)


# ── S1-04: cache prune TOCTOU ────────────────────────────────────────────────


async def _seed_old_entry(manager: LLMCacheManager, *, age_seconds: float) -> LLMCacheScope:
    """Insert a cache metadata entry old enough to be pruned; return its scope."""
    import time

    scope = manager.build_scope(
        user_id="u1",
        conversation_id="default",
        agent_id="main",
        provider_name="llamacpp",
        model="gpt-oss",
        preset="default",
        system="system",
        tools=[],
    )
    now = time.time()
    metadata = LLMCacheMetadata(
        scope=scope,
        filename=scope.filename,
        token_count=1000,
        file_size_bytes=100_000,
        prompt_tokens=1000,
        cached_tokens=800,
        prompt_n=200,
        created_at=now - age_seconds,
        last_used_at=now - age_seconds,
        last_saved_at=now - age_seconds,
        save_count=1,
        restore_count=0,
    )
    await cast(Any, manager)._store.upsert(metadata)
    return scope


@pytest.mark.asyncio
async def test_prune_deletes_old_entry_when_not_active(tmp_path: Path) -> None:
    """Baseline: prune removes an old entry when the scope is not active."""
    client = FakeSlotCacheClient()
    manager = LLMCacheManager(
        _config(tmp_path),
        provider_base_urls={"llamacpp": "http://llama:8080/v1"},
        client=client,
    )
    scope = await _seed_old_entry(manager, age_seconds=31 * 24 * 3600)

    await manager.prune()

    store = cast(Any, manager)._store
    remaining = await store.list_all()
    assert not any(m.scope.key == scope.key for m in remaining)


@pytest.mark.asyncio
async def test_prune_skips_scope_that_became_active_mid_prune(tmp_path: Path) -> None:
    """S1-04: TOCTOU fix — prune must not delete a scope that became active
    between the active_keys snapshot and the per-entry re-check.

    Reproduces the real race: ``prune`` takes the active_keys snapshot, then
    iterates entries. The first entry's ``await _delete_cache_entry`` yields
    control; a concurrent ``prepare()`` registers the SECOND entry's scope as
    active during that await. By the time the second entry's re-check runs
    (after the first delete's await), the scope is active and the re-check must
    skip it.

    Without the S1-04 re-check, the second entry is deleted (the snapshot missed
    the late activation), so this test FAILS when the re-check is removed.
    """
    client = FakeSlotCacheClient()
    manager = LLMCacheManager(
        _config(tmp_path),
        provider_base_urls={"llamacpp": "http://llama:8080/v1"},
        client=client,
    )
    # Two old entries: "first" will be deleted (its delete await yields control,
    # during which we activate "second"'s scope); "second" must survive.
    first_scope = manager.build_scope(
        user_id="u_first",
        conversation_id="default",
        agent_id="main",
        provider_name="llamacpp",
        model="gpt-oss",
        preset="default",
        system="s1",
        tools=[],
    )
    second_scope = manager.build_scope(
        user_id="u_second",
        conversation_id="default",
        agent_id="main",
        provider_name="llamacpp",
        model="gpt-oss",
        preset="default",
        system="s2",
        tools=[],
    )
    import time

    from corpclaw_lite.llm.cache import LLMCacheMetadata

    now = time.time()
    age = 31 * 24 * 3600
    for scope in (first_scope, second_scope):
        await cast(Any, manager)._store.upsert(
            LLMCacheMetadata(
                scope=scope,
                filename=scope.filename,
                token_count=1000,
                file_size_bytes=100_000,
                prompt_tokens=1000,
                cached_tokens=800,
                prompt_n=200,
                created_at=now - age,
                last_used_at=now - age,
                last_saved_at=now - age,
                save_count=1,
                restore_count=0,
            )
        )

    # Wrap _delete_cache_entry: when the FIRST entry is deleted, register the
    # second scope active (simulating a concurrent prepare() during the await),
    # then perform the real delete. The second entry's re-check then sees the
    # active scope and skips it.
    real_delete = manager._delete_cache_entry

    async def racing_delete(entry: LLMCacheMetadata) -> bool:
        if entry.scope.key == first_scope.key:
            # Yield control so this is a real suspension point, then activate
            # the second scope as a concurrent prepare() would.
            await asyncio.sleep(0)
            cast(Any, manager)._slot_scopes[0] = second_scope
        return await real_delete(entry)

    cast(Any, manager)._delete_cache_entry = racing_delete  # type: ignore[method-assign]

    await manager.prune()

    # first was deleted (its delete completed normally); second survived because
    # its scope was activated during first's delete await — so by the time the
    # second entry's re-check ran, the scope was active and the delete was
    # skipped. Without the S1-04 re-check, second would have been deleted too.
    store = cast(Any, manager)._store
    remaining = await store.list_all()
    remaining_keys = {m.scope.key for m in remaining}
    assert first_scope.key not in remaining_keys  # genuinely deleted
    assert second_scope.key in remaining_keys  # survived via late-activation re-check
