"""Live smoke for B-092 TokenizerClient against real llama-server /tokenize.

Skipped unless ``CORPCLAW_LIVE_LLM_TESTS=1`` (see ``tests/live_llm/conftest.py``).
Not part of default CI.
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import LiveLlmConfig

from corpclaw_lite.llm.tokenizer_client import TokenizerClient, estimate_tokens_heuristic


@pytest.mark.live_llm
@pytest.mark.asyncio
async def test_live_tokenize_roundtrip(
    live_config: LiveLlmConfig,
    report_writer: Any,
) -> None:
    """POST real /tokenize; compare to heuristic (loose factor, not equality)."""
    client = TokenizerClient(
        base_url=live_config.base_url,
        api_key=live_config.api_key,
        model=live_config.model,
        mode="tokenize",
        timeout_seconds=30.0,
    )
    samples = {
        "ascii_short": "Hello, CorpClaw Lite tokenizer smoke test.",
        "cyrillic": "Привет, это проверка токенизации кириллицы для B-092.",
        "mixed": "Report Q1 2026: выручка выросла на 12% year-over-year.",
    }
    rows: list[dict[str, Any]] = []
    for name, text in samples.items():
        estimate = await client.estimate(text)
        heuristic = estimate_tokens_heuristic(text)
        assert estimate.source in ("tokenize", "cache")
        assert estimate.approximate is False
        assert estimate.n_tokens > 0
        # Heuristic is rough; allow wide band so model/tokenizer drift does not flake.
        if heuristic > 0:
            ratio = estimate.n_tokens / heuristic
            assert 0.2 <= ratio <= 5.0, (
                f"{name}: tokenize={estimate.n_tokens} heuristic={heuristic} ratio={ratio:.2f}"
            )
        rows.append(
            {
                "name": name,
                "n_tokens": estimate.n_tokens,
                "heuristic": heuristic,
                "source": estimate.source,
            }
        )

    # Cache: second call must not require server (source=cache)
    again = await client.estimate(samples["ascii_short"])
    assert again.source == "cache"
    assert again.n_tokens == rows[0]["n_tokens"]

    report_writer(
        "tokenize_roundtrip",
        {
            "base_url": live_config.base_url,
            "model": live_config.model,
            "samples": rows,
        },
    )
