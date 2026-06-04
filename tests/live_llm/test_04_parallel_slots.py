from __future__ import annotations

import asyncio
import time

import pytest
from helpers import LiveLlamaClient, LiveLlmConfig, erase_slots, generate_prompt

pytestmark = [pytest.mark.live_llm, pytest.mark.llm_required]


@pytest.mark.asyncio
async def test_four_parallel_slot_requests(
    live_client: LiveLlamaClient,
    live_config: LiveLlmConfig,
    report_writer,
) -> None:
    assert len(live_config.slots) >= 4
    slots = live_config.slots[:4]
    await erase_slots(live_client, slots)
    prompts = [
        generate_prompt(live_config.prompt_tokens, label=f"PARALLEL_{slot}") for slot in slots
    ]

    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            live_client.chat_streamed(slot_id=slot_id, prompt=prompt)
            for slot_id, prompt in zip(slots, prompts, strict=True)
        )
    )
    wall_s = time.perf_counter() - started
    await erase_slots(live_client, slots)

    report_writer(
        "test_04_parallel_slots",
        {
            "slots": slots,
            "wall_s": wall_s,
            "results": results,
            "sum_total_s": sum(result.total_s for result in results),
        },
    )

    for result in results:
        assert result.status == 200, result.error
        assert result.prompt_tokens > 0
        assert result.prompt_n > 0
        assert result.predicted_n > 0
    assert wall_s < sum(result.total_s for result in results) * 0.75
