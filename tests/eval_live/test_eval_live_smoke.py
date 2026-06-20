"""Live smoke test for the eval harness (B-060).

Runs ONE simple scenario end-to-end against a real llama-server to verify the
plumbing (AgentStack build → AgentLoop.run → trajectory → scoring → report)
works with a live model. This is NOT a quality benchmark — it just confirms the
harness does not crash on a real inference call. Single-pass, no judge, no A/B
to keep it fast.

Gated behind CORPCLAW_EVAL_LIVE=1 (see conftest.py). Configure the live server
via CORPCLAW_LIVE_LLM_BASE_URL / CORPCLAW_LIVE_LLM_MODEL.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


@pytest.mark.eval_live
@pytest.mark.asyncio
async def test_eval_live_smoke_single_scenario(tmp_path: Path) -> None:
    """One exact-match scenario against the live model; report must be written."""
    base_url = os.environ.get("CORPCLAW_LIVE_LLM_BASE_URL", "http://192.168.193.178:8080")
    model = os.environ.get("CORPCLAW_LIVE_LLM_MODEL", "gpt-oss-20b-UD-Q4_K_XL")

    # Configure a 'live' provider pointing at the llama-server.
    provider_env = {
        "PROVIDER_LIVE__TYPE": "openai",
        "PROVIDER_LIVE__BASE_URL": base_url,
        "PROVIDER_LIVE__API_KEY": os.environ.get("CORPCLAW_LIVE_LLM_API_KEY", "dummy"),
    }

    # Rewrite settings so the stack uses the live provider with containers off.
    from corpclaw_lite.config import loader as config_loader
    from corpclaw_lite.config.settings import (
        ContainerSettings,
        LLMSettings,
        RoutingRule,
    )

    _original_load = config_loader.load_settings

    def _mock_load(path: object = None) -> Any:  # type: ignore[misc]
        settings = _original_load(path)  # type: ignore[arg-type]
        settings.container = ContainerSettings(enabled=False)
        settings.llm = LLMSettings(
            routing=[
                RoutingRule(task_kind="default", provider="live", model=model),
            ]
        )
        return settings

    # Write a one-scenario corpus.
    import yaml

    corpus = tmp_path / "smoke.yaml"
    corpus.write_text(
        yaml.safe_dump(
            {
                "scenarios": [
                    {
                        "id": "smoke_add",
                        "category": "smoke",
                        "user_message": "Сколько будет 2 + 2? Назови только число.",
                        "expected_answer": "4",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    from corpclaw_lite.eval.loop import EvalLoop

    with (
        patch.dict(os.environ, provider_env, clear=False),
        patch.object(config_loader, "load_settings", side_effect=_mock_load),
    ):
        # EvalLoop imports load_settings lazily, so patch at its source module too.
        import corpclaw_lite.eval.loop as eval_loop_module

        with patch.object(eval_loop_module, "load_settings", side_effect=_mock_load):
            ev = EvalLoop(
                scenarios_path=corpus,
                judge=None,
                ab_guards=False,
                output_dir=tmp_path / "out",
                workspace_base=tmp_path / "ws",
            )
            report = await ev.run()

    assert report is not None
    assert (tmp_path / "out" / "eval_report.json").exists()
    assert (tmp_path / "out" / "eval_report.md").exists()
    # The smoke test asserts plumbing, not pass/fail — a local model may answer
    # wrong, but the run must complete and emit a report.
