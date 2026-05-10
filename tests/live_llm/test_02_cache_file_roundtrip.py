from __future__ import annotations

import pytest
from helpers import LiveLlamaClient, LiveLlmConfig, generate_prompt, managed_cache_filename

pytestmark = [pytest.mark.live_llm, pytest.mark.llm_required]


@pytest.mark.asyncio
async def test_cache_file_roundtrip_1k(
    live_client: LiveLlamaClient,
    live_config: LiveLlmConfig,
    report_writer,
) -> None:
    await _run_roundtrip(
        live_client,
        live_config,
        report_writer,
        test_name="test_02_cache_file_roundtrip_1k",
        target_tokens=live_config.prompt_tokens,
    )


@pytest.mark.live_llm_slow
@pytest.mark.asyncio
async def test_cache_file_roundtrip_large(
    live_client: LiveLlamaClient,
    live_config: LiveLlmConfig,
    report_writer,
) -> None:
    await _run_roundtrip(
        live_client,
        live_config,
        report_writer,
        test_name="test_02_cache_file_roundtrip_large",
        target_tokens=live_config.large_prompt_tokens,
    )


async def _run_roundtrip(
    live_client: LiveLlamaClient,
    live_config: LiveLlmConfig,
    report_writer,
    *,
    test_name: str,
    target_tokens: int,
) -> None:
    slot_id = live_config.slots[0]
    filename = managed_cache_filename(test_name, slot_id)
    prompt = generate_prompt(target_tokens, label="ROUNDTRIP")
    cleanup_deleted = False

    try:
        erase_before = await live_client.slot_erase(slot_id)
        cold = await live_client.chat_streamed(slot_id=slot_id, prompt=prompt)
        save = await live_client.slot_save(slot_id, filename)
        erase_after = await live_client.slot_erase(slot_id)
        restore = await live_client.slot_restore(slot_id, filename)
        warm = await live_client.chat_streamed(slot_id=slot_id, prompt=prompt)
    finally:
        await live_client.slot_erase(slot_id)
        cleanup_deleted = await live_client.delete_managed_cache_file(filename)

    report_writer(
        test_name,
        {
            "filename": filename,
            "cleanup_deleted": cleanup_deleted,
            "erase_before": erase_before,
            "cold": cold,
            "save": save,
            "erase_after": erase_after,
            "restore": restore,
            "warm": warm,
            "warm_cache_reuse_ratio": warm.cache_reuse_ratio,
        },
    )

    assert erase_before.ok
    assert cold.status == 200, cold.error
    assert save.ok, save.error
    assert erase_after.ok
    assert restore.ok, restore.error
    assert warm.status == 200, warm.error
    assert cold.cached_tokens == 0
    assert warm.cache_reuse_ratio >= 0.70
    assert warm.prompt_n < cold.prompt_n
    assert warm.prompt_ms < cold.prompt_ms
    assert warm.ttft_any_s is not None
    assert cold.ttft_any_s is not None
    assert warm.ttft_any_s < cold.ttft_any_s
