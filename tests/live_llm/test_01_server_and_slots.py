from __future__ import annotations

import pytest
from helpers import LiveLlamaClient, LiveLlmConfig

pytestmark = [pytest.mark.live_llm, pytest.mark.llm_required]


@pytest.mark.asyncio
async def test_server_models_and_slots(
    live_client: LiveLlamaClient,
    live_config: LiveLlmConfig,
    report_writer,
) -> None:
    models = await live_client.get_models()
    slots = await live_client.get_slots()
    missing_slots = [slot_id for slot_id in live_config.slots if slot_id not in slots]

    report_writer(
        "test_01_server_and_slots",
        {
            "base_url": live_config.base_url,
            "model": live_config.model,
            "models": models,
            "slots": slots,
            "configured_slots": live_config.slots,
            "missing_slots": missing_slots,
        },
    )

    assert live_config.model in models
    assert len(slots) >= len(live_config.slots)
    assert not missing_slots
