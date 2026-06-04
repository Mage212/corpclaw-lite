from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.llm.base import LLMResponse, TokenUsage
from corpclaw_lite.llm.cache import LLMCacheManager, PersistentCacheConfig, SlotCacheActionResult
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
