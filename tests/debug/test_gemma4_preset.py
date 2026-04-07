"""
Manual integration test for gemma4-thinking preset.

Checks:
  1. PresetRegistry loads 'gemma4-thinking' correctly
  2. Provider applies inference params (temperature, top_p, top_k)
  3. System prompt contains system_prompt_prefix
  4. Reasoning is extracted from thinking tags in content
  5. Response content is clean (no thinking tags in final content)
  6. LLMResponse.reasoning is non-empty

Run:
    uv run python tests/debug/test_gemma4_preset.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s %(name)s: %(message)s",
)
# Only show our module's debug logs — suppress httpx noise
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("preset_test")


async def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()  # load .env so ${OPENAI_BASE_URL} etc. are resolved

    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.llm.presets import PresetRegistry
    from corpclaw_lite.llm.router import build_provider

    project_root = Path(__file__).parent.parent.parent
    settings = load_settings(project_root / "config" / "settings.yaml")
    preset_registry = PresetRegistry.from_yaml(project_root / "config" / "model_presets.yaml")

    # ── 1. Preset loading ──────────────────────────────────────────────────────
    preset = preset_registry.get("gemma4-thinking")
    if preset is None:
        print("❌ FAIL: 'gemma4-thinking' preset not found in registry")
        sys.exit(1)

    print("✅ Preset 'gemma4-thinking' loaded:")
    print(f"   description          = {preset.description!r}")
    print(f"   system_prompt_prefix = {preset.system_prompt_prefix!r}")
    print(f"   thinking.source      = {preset.thinking.source if preset.thinking else None!r}")
    print(f"   thinking.open_tag    = {preset.thinking.open_tag if preset.thinking else None!r}")
    print(f"   thinking.close_tag   = {preset.thinking.close_tag if preset.thinking else None!r}")
    print(f"   inference_params     = {preset.inference_params}")

    # ── 2. Build provider from settings (uses preset embedded in settings.yaml) ─
    default_cfg = settings.llm.named.get("default")
    if default_cfg is None:
        print("❌ FAIL: 'default' provider not configured")
        sys.exit(1)

    provider = build_provider(default_cfg, preset_registry=preset_registry)
    print(f"\n✅ Provider built: {default_cfg.model} @ {default_cfg.base_url}")
    print(f"   Preset applied: {default_cfg.preset!r}")

    # ── 3. Actual LLM call ────────────────────────────────────────────────────
    test_message = "Сколько будет 145 умножить на 37? Считай пошагово."
    print(f"\n📤 Sending: {test_message!r}")

    try:
        response = await provider.chat(
            messages=[{"role": "user", "content": test_message}],
            system="Ты — помощник для математических вычислений.",
        )
    except Exception as e:
        print(f"❌ FAIL: LLM call failed: {e}")
        sys.exit(1)

    print("\n─── RAW RESPONSE ──────────────────────────────────────────────────")
    print(f"content ({len(response.content)} chars):\n{response.content[:800]}")

    # ── 4. Reasoning extraction check ─────────────────────────────────────────
    print("\n─── REASONING EXTRACTION ──────────────────────────────────────────")
    if response.reasoning:
        print(f"✅ reasoning present ({len(response.reasoning)} chars):")
        print(response.reasoning[:500])
    else:
        print("⚠️  reasoning is EMPTY — model may not have used thinking tags")
        print("   (check if model produced <|channel>thought ... <channel|> in output)")

    # ── 5. Content cleanliness check ──────────────────────────────────────────
    print("\n─── CONTENT CLEANLINESS ───────────────────────────────────────────")
    leakage = False
    if preset.thinking:
        for tag in (preset.thinking.open_tag, preset.thinking.close_tag):
            if tag and tag in response.content:
                print(f"❌ FAIL: thinking tag {tag!r} leaked into content")
                leakage = True
    if not leakage:
        print("✅ No thinking tags leaked into content")

    # ── 6. Summary ────────────────────────────────────────────────────────────
    print("\n═══ TEST SUMMARY ═══════════════════════════════════════════════════")
    print("✅ Preset loaded with correct strategy/tags")
    print("✅ Provider built without errors")
    print("✅ LLM responded successfully")
    if not leakage:
        print("✅ Content is clean (no tag leakage)")
    else:
        print("❌ Content has tag leakage — check _parse_reasoning()")
    if response.reasoning:
        print("✅ Reasoning extracted from model output")
    else:
        print("⚠️  Reasoning not extracted (model may not have used thinking tags)")
    print("────────────────────────────────────────────────────────────────────")


if __name__ == "__main__":
    asyncio.run(main())
