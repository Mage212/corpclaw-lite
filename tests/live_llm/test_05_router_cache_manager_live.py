from __future__ import annotations

from pathlib import Path

import pytest
from helpers import LiveLlamaClient, LiveLlmConfig, generate_prompt

from corpclaw_lite.llm.cache import LLMCacheManager, PersistentCacheConfig
from corpclaw_lite.llm.queue import LLMRequestQueue, SlotAffinityConfig

pytestmark = [pytest.mark.live_llm, pytest.mark.llm_required]


@pytest.mark.asyncio
async def test_agent_scoped_cache_manager_live(
    live_client: LiveLlamaClient,
    live_config: LiveLlmConfig,
    tmp_path: Path,
    report_writer,
) -> None:
    slot_id = live_config.slots[0]
    manager = LLMCacheManager(
        PersistentCacheConfig(
            enabled=True,
            root_dir=tmp_path / "slot-cache",
            index_path=tmp_path / "index.sqlite",
            save_min_tokens=1,
            save_dirty_seconds=0,
            save_policy="eviction_only",
        ),
        provider_base_urls={"llamacpp": live_config.base_url},
        client=live_client,
    )
    queue = LLMRequestQueue(
        max_concurrent=1,
        strategy="slot_affinity",
        slot_affinity=SlotAffinityConfig(
            enabled=True,
            provider_names=("llamacpp",),
            sticky_slot_ids=(slot_id,),
            overflow_slot_ids=(),
        ),
    )

    main_scope = manager.build_scope(
        user_id="live-user",
        conversation_id="default",
        agent_id="main",
        provider_name="llamacpp",
        model=live_config.model,
        preset=None,
        system="main system",
        tools=[],
    )
    subagent_scope = manager.build_scope(
        user_id="live-user",
        conversation_id="default",
        agent_id="data-agent",
        provider_name="llamacpp",
        model=live_config.model,
        preset=None,
        system="data agent system",
        tools=[],
    )

    entry_main = await queue.acquire("live-user", provider_name="llamacpp")
    main_lease = await manager.prepare(entry_main, main_scope)
    main_response = await live_client.chat_streamed(
        slot_id=slot_id,
        prompt=generate_prompt(live_config.prompt_tokens, label="MAIN"),
    )
    await manager.finalize(entry_main, main_lease, _to_response(main_response))
    await queue.release(entry_main, main_response.total_s)

    entry_subagent = await queue.acquire(
        "live-user",
        task_kind="subagent:data-agent",
        load_class="subagent",
        provider_name="llamacpp",
    )
    subagent_lease = await manager.prepare(entry_subagent, subagent_scope)
    subagent_response = await live_client.chat_streamed(
        slot_id=slot_id,
        prompt=generate_prompt(live_config.prompt_tokens, label="SUBAGENT"),
    )
    await manager.finalize(entry_subagent, subagent_lease, _to_response(subagent_response))
    await queue.release(entry_subagent, subagent_response.total_s)
    await live_client.slot_erase(slot_id)

    report_writer(
        "test_05_router_cache_manager_live",
        {
            "main_scope_key": main_scope.key,
            "subagent_scope_key": subagent_scope.key,
            "main_filename": main_scope.filename,
            "subagent_filename": subagent_scope.filename,
            "main_response": main_response,
            "subagent_response": subagent_response,
        },
    )

    assert main_scope.key != subagent_scope.key
    assert main_scope.filename != subagent_scope.filename
    assert main_response.status == 200, main_response.error
    assert subagent_response.status == 200, subagent_response.error


def _to_response(metrics):
    from corpclaw_lite.llm.base import LLMResponse, TokenUsage

    return LLMResponse(
        content="ok",
        usage=TokenUsage(
            input_tokens=metrics.prompt_tokens,
            output_tokens=metrics.completion_tokens,
            cached_input_tokens=metrics.cached_tokens,
            prompt_processing_tokens=metrics.prompt_n,
            prompt_processing_ms=metrics.prompt_ms,
            predicted_tokens=metrics.predicted_n,
            predicted_ms=metrics.predicted_ms,
        ),
    )
