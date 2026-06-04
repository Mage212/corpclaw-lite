from __future__ import annotations

from pathlib import Path

import pytest
from helpers import (
    LiveLlamaClient,
    LiveLlmConfig,
    generate_prompt,
    managed_cache_filename,
)

from corpclaw_lite.llm.base import LLMResponse, TokenUsage
from corpclaw_lite.llm.cache import (
    LLMCacheManager,
    PersistentCacheConfig,
    SlotCacheActionResult,
)
from corpclaw_lite.llm.queue import LLMRequestQueue, SlotAffinityConfig

pytestmark = [pytest.mark.live_llm, pytest.mark.llm_required]


class FakeSlotCacheClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def save(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        self.calls.append("save")
        return SlotCacheActionResult(
            ok=True,
            status_code=200,
            action="save",
            slot_id=slot_id,
            filename=filename,
            n_tokens=1000,
            n_bytes=10_000,
        )

    async def restore(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        self.calls.append("restore")
        return SlotCacheActionResult(
            ok=True,
            status_code=200,
            action="restore",
            slot_id=slot_id,
            filename=filename,
            n_tokens=1000,
            n_bytes=10_000,
        )

    async def erase(self, slot_id: int, *, model: str) -> SlotCacheActionResult:
        self.calls.append("erase")
        return SlotCacheActionResult(
            ok=True,
            status_code=200,
            action="erase",
            slot_id=slot_id,
            n_tokens=1000,
        )


@pytest.mark.asyncio
async def test_restored_cache_mismatch_is_observable(
    live_client: LiveLlamaClient,
    live_config: LiveLlmConfig,
    report_writer,
) -> None:
    slot_id = live_config.slots[0]
    filename = managed_cache_filename("test_03_cache_mismatch", slot_id)
    prompt_a = generate_prompt(live_config.prompt_tokens, label="ALPHA")
    prompt_b = generate_prompt(live_config.prompt_tokens, label="OMEGA_UNRELATED_CONTEXT")
    cleanup_deleted = False

    try:
        await live_client.slot_erase(slot_id)
        cold_a = await live_client.chat_streamed(
            slot_id=slot_id,
            prompt=prompt_a,
            system="alpha",
        )
        save = await live_client.slot_save(slot_id, filename)
        await live_client.slot_erase(slot_id)
        restore = await live_client.slot_restore(slot_id, filename)
        mismatch = await live_client.chat_streamed(slot_id=slot_id, prompt=prompt_b, system="omega")
    finally:
        await live_client.slot_erase(slot_id)
        cleanup_deleted = await live_client.delete_managed_cache_file(filename)

    report_writer(
        "test_03_cache_mismatch_validation",
        {
            "filename": filename,
            "cleanup_deleted": cleanup_deleted,
            "cold_a": cold_a,
            "save": save,
            "restore": restore,
            "mismatch": mismatch,
            "mismatch_cache_reuse_ratio": mismatch.cache_reuse_ratio,
        },
    )

    assert cold_a.status == 200, cold_a.error
    assert save.ok, save.error
    assert restore.ok, restore.error
    assert mismatch.status == 200, mismatch.error
    assert mismatch.cache_reuse_ratio < 0.70
    assert mismatch.prompt_n > mismatch.prompt_tokens * 0.50


@pytest.mark.asyncio
async def test_cache_manager_strict_fallback_decision(tmp_path: Path) -> None:
    fake_client = FakeSlotCacheClient()
    config = PersistentCacheConfig(
        enabled=True,
        root_dir=tmp_path / "slot-cache",
        index_path=tmp_path / "index.sqlite",
        save_min_tokens=1,
        validation_min_reuse_ratio=0.70,
        strict_mismatch_retry=True,
    )
    scope_kwargs = {
        "user_id": "live-user",
        "conversation_id": "default",
        "agent_id": "main",
        "provider_name": "llamacpp",
        "model": "model",
        "preset": None,
        "system": "system",
        "tools": [],
    }

    first_manager = LLMCacheManager(
        config,
        provider_base_urls={"llamacpp": "http://example.invalid"},
        client=fake_client,
    )
    first_queue = _single_slot_queue(0)
    first_entry = await first_queue.acquire("live-user", provider_name="llamacpp")
    scope = first_manager.build_scope(**scope_kwargs)
    first_lease = await first_manager.prepare(first_entry, scope)
    await first_manager.finalize(first_entry, first_lease, _response(cached=0, prompt_n=1000))
    await first_queue.release(first_entry, 1.0)

    second_manager = LLMCacheManager(
        config,
        provider_base_urls={"llamacpp": "http://example.invalid"},
        client=fake_client,
    )
    second_queue = _single_slot_queue(0)
    second_entry = await second_queue.acquire("live-user", provider_name="llamacpp")
    second_lease = await second_manager.prepare(second_entry, scope)
    result = await second_manager.finalize(
        second_entry,
        second_lease,
        _response(cached=100, prompt_n=900),
    )
    await second_queue.release(second_entry, 1.0)

    assert second_lease.hit_kind == "l2"
    assert result.retry_without_cache is True
    assert result.mismatch_reason == "low_cache_reuse_ratio"
    assert "restore" in fake_client.calls
    assert "erase" in fake_client.calls


def _single_slot_queue(slot_id: int) -> LLMRequestQueue:
    return LLMRequestQueue(
        max_concurrent=1,
        strategy="slot_affinity",
        slot_affinity=SlotAffinityConfig(
            enabled=True,
            provider_names=("llamacpp",),
            sticky_slot_ids=(slot_id,),
            overflow_slot_ids=(),
        ),
    )


def _response(*, cached: int, prompt_n: int) -> LLMResponse:
    return LLMResponse(
        content="ok",
        usage=TokenUsage(
            input_tokens=1000,
            output_tokens=10,
            cached_input_tokens=cached,
            prompt_processing_tokens=prompt_n,
        ),
    )
