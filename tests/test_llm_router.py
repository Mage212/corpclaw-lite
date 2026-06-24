"""Tests for LLMRouter — routing, caching, validation, and fallback logic.

Covers:
  - from_settings() with multiple routing rules
  - for_task() routing and fallback to default
  - for_subagent() routing and fallback to default
  - Validation: unknown provider, missing model, no default rule
  - Provider instance caching per (provider_name, model, preset)
  - Provider protocol delegation (chat, stream, chat_with_image)
  - build_provider() for anthropic and openai types
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.config.providers import ProviderConnection, ProviderRegistry
from corpclaw_lite.config.settings import LLMSettings, RoutingRule
from corpclaw_lite.llm.base import LLMResponse
from corpclaw_lite.llm.presets import ModelPreset, PresetRegistry
from corpclaw_lite.llm.router import LLMRouter, build_provider

# ── Helpers ────────────────────────────────────────────────────────────────────

_OLLAMA_ENV = {
    "PROVIDER_OLLAMA__TYPE": "openai",
    "PROVIDER_OLLAMA__BASE_URL": "http://localhost:11434/v1",
    "PROVIDER_OLLAMA__API_KEY": "ollama",
}

_ANTHROPIC_ENV = {
    "PROVIDER_ANTHROPIC__TYPE": "anthropic",
    "PROVIDER_ANTHROPIC__API_KEY": "sk-ant-test",
}

_MULTI_ENV = {**_OLLAMA_ENV, **_ANTHROPIC_ENV}


def _make_registry(env: dict[str, str] | None = None) -> ProviderRegistry:
    return ProviderRegistry.from_env(env or _MULTI_ENV)


def _make_settings(rules: list[RoutingRule]) -> LLMSettings:
    return LLMSettings(routing=rules, queue={"enabled": False})


# ── build_provider ─────────────────────────────────────────────────────────────


def test_build_provider_openai() -> None:
    """OpenAI-type provider builds successfully."""
    conn = ProviderConnection(type="openai", base_url="http://localhost:11434/v1", api_key="key")
    provider = build_provider(conn, model="test-model")
    assert provider is not None
    # Should be an OpenAIProvider
    from corpclaw_lite.llm.openai import OpenAIProvider

    assert isinstance(provider, OpenAIProvider)


def test_build_provider_anthropic() -> None:
    """Anthropic provider builds with API key."""
    conn = ProviderConnection(type="anthropic", api_key="sk-ant-test")
    provider = build_provider(conn, model="claude-3-haiku")
    assert provider is not None
    from corpclaw_lite.llm.anthropic import AnthropicProvider

    assert isinstance(provider, AnthropicProvider)


def test_build_provider_anthropic_no_key_returns_none() -> None:
    """Anthropic without API key returns None."""
    conn = ProviderConnection(type="anthropic", api_key=None)
    provider = build_provider(conn, model="claude-3-haiku")
    assert provider is None


def test_build_provider_with_preset() -> None:
    """Preset is passed through to provider."""
    conn = ProviderConnection(type="openai", base_url="http://localhost:11434/v1")
    preset = ModelPreset(inference_params={"temperature": 0.9})
    provider = build_provider(conn, model="test", preset=preset)
    assert provider is not None
    assert provider._preset is preset  # type: ignore[attr-defined]


# ── from_settings — basic routing ──────────────────────────────────────────────


def test_from_settings_single_default_rule() -> None:
    """Single routing rule with task_kind='default' creates router."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)
    assert router.default is not None


def test_from_settings_multiple_tasks() -> None:
    """Multiple routing rules map different tasks to different providers."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="vision", provider="ollama", model="glm-ocr"),
            RoutingRule(task_kind="consolidate", provider="anthropic", model="claude-3-haiku"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    # Default provider
    default = router.for_task("default")
    assert default is not None

    # Vision task → different model, same provider
    vision = router.for_task("vision")
    assert vision is not None
    assert vision is not default  # different (model, preset) → different instance

    # Consolidate task → different provider entirely
    consolidate = router.for_task("consolidate")
    assert consolidate is not None
    assert consolidate is not default


def test_from_settings_with_subagent_routing() -> None:
    """Routing by subagent_id works alongside task_kind."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(subagent_id="research-agent", provider="anthropic", model="claude-3-haiku"),
            RoutingRule(subagent_id="exec-agent", provider="ollama", model="qwen3.5-4b"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    # Subagent routing
    research = router.for_subagent("research-agent")
    assert research is not None
    from corpclaw_lite.llm.anthropic import AnthropicProvider

    assert isinstance(research, AnthropicProvider)

    # Exec-agent uses ollama
    exec_prov = router.for_subagent("exec-agent")
    from corpclaw_lite.llm.openai import OpenAIProvider

    assert isinstance(exec_prov, OpenAIProvider)


# ── for_task / for_subagent — fallback ─────────────────────────────────────────


def test_for_task_unknown_returns_default() -> None:
    """Unknown task_kind falls back to default provider."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    unknown = router.for_task("nonexistent-task")
    assert unknown is router.default


def test_for_subagent_unknown_returns_default() -> None:
    """Unknown subagent_id falls back to default provider."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    unknown = router.for_subagent("nonexistent-agent")
    assert unknown is router.default


# ── Validation ─────────────────────────────────────────────────────────────────


def test_unknown_provider_in_rule_skipped() -> None:
    """Routing rule with unknown provider is skipped, others still work."""
    registry = _make_registry()  # has ollama + anthropic
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(
                task_kind="vision",
                provider="nonexistent",  # ← doesn't exist
                model="some-model",
            ),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    # Default works
    assert router.default is not None

    # Vision falls back to default (rule was skipped)
    assert router.for_task("vision") is router.default


def test_missing_model_in_rule_skipped() -> None:
    """Routing rule without model is skipped."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="vision", provider="ollama"),  # ← no model
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    # Vision falls back to default (rule was skipped)
    assert router.for_task("vision") is router.default


def test_no_default_rule_uses_first_as_fallback() -> None:
    """Without task_kind='default', first valid rule becomes default."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="vision", provider="ollama", model="glm-ocr"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    # Default should be the vision provider (first rule)
    assert router.default is not None
    assert router.default is router.for_task("vision")


def test_no_valid_rules_raises_error() -> None:
    """No valid routing rules raises RuntimeError."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="nonexistent", model="test"),
        ]
    )
    with pytest.raises(RuntimeError, match="No valid routing rules found"):
        LLMRouter.from_settings(settings, registry)


def test_anthropic_without_key_skipped() -> None:
    """Anthropic provider without API key is skipped (returns None from build)."""
    env = {
        "PROVIDER_OLLAMA__TYPE": "openai",
        "PROVIDER_OLLAMA__BASE_URL": "http://localhost:11434/v1",
        "PROVIDER_OLLAMA__API_KEY": "ollama",
        "PROVIDER_ANTHROPIC__TYPE": "anthropic",
        # No API_KEY → build_provider returns None
    }
    registry = ProviderRegistry.from_env(env)
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="vision", provider="anthropic", model="claude-3-haiku"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    # Vision rule was skipped (provider couldn't be built), falls back to default
    assert router.for_task("vision") is router.default


# ── Caching ────────────────────────────────────────────────────────────────────


def test_same_provider_model_preset_cached() -> None:
    """Same (provider, model, preset) returns the same Provider instance."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="vision", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(subagent_id="research-agent", provider="ollama", model="qwen3.5-4b"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    # All three should return the SAME provider instance (cached)
    default = router.for_task("default")
    vision = router.for_task("vision")
    research = router.for_subagent("research-agent")
    assert default is vision
    assert vision is research


def test_different_models_different_instances() -> None:
    """Different models → different Provider instances."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="vision", provider="ollama", model="glm-ocr"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    assert router.for_task("default") is not router.for_task("vision")


def test_different_providers_different_instances() -> None:
    """Different providers (same model name) → different instances."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="test-model"),
            RoutingRule(task_kind="vision", provider="anthropic", model="test-model"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    default = router.for_task("default")
    vision = router.for_task("vision")
    assert default is not vision
    from corpclaw_lite.llm.anthropic import AnthropicProvider
    from corpclaw_lite.llm.openai import OpenAIProvider

    assert isinstance(default, OpenAIProvider)
    assert isinstance(vision, AnthropicProvider)


# ── Preset integration ─────────────────────────────────────────────────────────


def test_preset_applied_from_routing_rule(tmp_path: None) -> None:  # type: ignore[misc]
    """Routing rule with preset name → preset resolved and passed to provider."""
    import textwrap
    from pathlib import Path

    registry = _make_registry()

    # Create a temp preset file
    from corpclaw_lite.llm.presets import PresetRegistry

    preset_yaml = textwrap.dedent("""\
        presets:
          test-preset:
            description: "Test preset"
            inference_params:
              temperature: 0.42
    """)
    preset_path = Path("/tmp/_test_presets_router.yaml")
    preset_path.write_text(preset_yaml, encoding="utf-8")
    try:
        preset_reg = PresetRegistry.from_yaml(preset_path)
        settings = _make_settings(
            [
                RoutingRule(
                    task_kind="default",
                    provider="ollama",
                    model="qwen3.5-4b",
                    preset="test-preset",
                ),
            ]
        )
        router = LLMRouter.from_settings(settings, registry, preset_reg)
        provider = router.default
        assert provider is not None
        assert provider._preset is not None  # type: ignore[attr-defined]
        assert provider._preset.inference_params["temperature"] == 0.42  # type: ignore[attr-defined]
    finally:
        preset_path.unlink(missing_ok=True)


def test_sampling_rule_resolves_split_profiles(tmp_path: None) -> None:  # type: ignore[misc]
    """D-056 PR2: routing rule with sampling: → SamplingProfile resolved on provider."""
    import textwrap
    from pathlib import Path

    registry = _make_registry()

    # New split format: a sampling profile referencing a model profile.
    from corpclaw_lite.llm.presets import PresetRegistry

    preset_yaml = textwrap.dedent("""\
        models:
          qwen-test:
            default_inference:
              temperature: 0.7
              top_k: 20
        sampling:
          fast-off:
            model: qwen-test
            thinking_mode: off
            inference_overrides:
              temperature: 0.2
    """)
    preset_path = Path("/tmp/_test_sampling_router.yaml")
    preset_path.write_text(preset_yaml, encoding="utf-8")
    try:
        preset_reg = PresetRegistry.from_yaml(preset_path)
        settings = _make_settings(
            [
                RoutingRule(
                    task_kind="default",
                    provider="ollama",
                    model="qwen3.5-4b",
                    sampling="fast-off",
                ),
            ]
        )
        router = LLMRouter.from_settings(settings, registry, preset_reg)
        provider = router.default
        assert provider is not None
        # Model profile resolved from the sampling profile's model reference.
        assert provider._model_profile is not None  # type: ignore[attr-defined]
        assert provider._model_profile.default_inference["temperature"] == 0.7  # type: ignore[attr-defined]
        # Sampling profile resolved with the configured thinking_mode.
        assert provider._sampling is not None  # type: ignore[attr-defined]
        assert provider._sampling.thinking_mode == "off"  # type: ignore[attr-defined]
        assert provider._sampling.inference_overrides["temperature"] == 0.2  # type: ignore[attr-defined]
        # No legacy preset (new-style rule).
        assert provider._preset is None  # type: ignore[attr-defined]
    finally:
        preset_path.unlink(missing_ok=True)


def test_sampling_wins_over_legacy_preset_field() -> None:
    """When a rule has both sampling and preset, sampling wins (D-056)."""
    import textwrap
    from pathlib import Path

    from corpclaw_lite.llm.presets import PresetRegistry

    registry = _make_registry()
    preset_yaml = textwrap.dedent("""\
        models:
          qwen-test:
            default_inference:
              temperature: 0.7
        sampling:
          sampling-rule:
            model: qwen-test
            thinking_mode: off
        presets:
          legacy-rule:
            inference_params:
              temperature: 0.9
    """)
    preset_path = Path("/tmp/_test_sampling_wins.yaml")
    preset_path.write_text(preset_yaml, encoding="utf-8")
    try:
        preset_reg = PresetRegistry.from_yaml(preset_path)
        settings = _make_settings(
            [
                RoutingRule(
                    task_kind="default",
                    provider="ollama",
                    model="qwen3.5-4b",
                    sampling="sampling-rule",
                    preset="legacy-rule",  # legacy, ignored in favor of sampling
                ),
            ]
        )
        router = LLMRouter.from_settings(settings, registry, preset_reg)
        provider = router.default
        assert provider is not None
        # Sampling resolved → thinking off.
        assert provider._sampling is not None  # type: ignore[attr-defined]
        assert provider._sampling.thinking_mode == "off"  # type: ignore[attr-defined]
    finally:
        preset_path.unlink(missing_ok=True)


def test_unknown_preset_name_ignored() -> None:
    """Routing rule with unknown preset name → preset is None, provider still built."""
    registry = _make_registry()
    preset_reg = PresetRegistry()  # empty — no presets

    settings = _make_settings(
        [
            RoutingRule(
                task_kind="default",
                provider="ollama",
                model="qwen3.5-4b",
                preset="nonexistent-preset",
            ),
        ]
    )
    router = LLMRouter.from_settings(settings, registry, preset_reg)
    assert router.default is not None
    assert router.default._preset is None  # type: ignore[attr-defined]


def test_no_preset_registry() -> None:
    """No preset_registry → presets are ignored, providers built without presets."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(
                task_kind="default",
                provider="ollama",
                model="qwen3.5-4b",
                preset="some-preset",
            ),
        ]
    )
    router = LLMRouter.from_settings(settings, registry, preset_registry=None)
    assert router.default is not None
    assert router.default._preset is None  # type: ignore[attr-defined]


# ── Router as Provider (delegation) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_chat_delegates_to_default() -> None:
    """router.chat() delegates to the default provider's chat()."""
    mock_provider = MagicMock()
    expected = LLMResponse(content="hello")
    mock_provider.chat = AsyncMock(return_value=expected)

    router = LLMRouter(
        providers={"ollama:test": mock_provider},
        default_provider=mock_provider,
        default_provider_name="ollama",
        routing=[("default", None, mock_provider, "ollama")],
    )

    result = await router.chat(messages=[{"role": "user", "content": "hi"}])
    assert result is expected
    mock_provider.chat.assert_awaited_once()


# ── default property ───────────────────────────────────────────────────────────


def test_default_property() -> None:
    """default property returns the default provider."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    assert router.default is not None
    assert router.default is router.for_task("default")


# ── Production task_kinds ──────────────────────────────────────────────────────


def test_consolidate_routing() -> None:
    """for_task('consolidate') returns a dedicated provider when rule exists."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="consolidate", provider="anthropic", model="claude-3-haiku"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    consolidate = router.for_task("consolidate")
    assert consolidate is not None
    assert consolidate is not router.default

    from corpclaw_lite.llm.anthropic import AnthropicProvider

    assert isinstance(consolidate, AnthropicProvider)


def test_compress_routing() -> None:
    """for_task('compress') returns a dedicated provider when rule exists."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="compress", provider="ollama", model="small-model"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    compress = router.for_task("compress")
    assert compress is not None
    assert compress is not router.default


def test_calibration_routing() -> None:
    """for_task('calibration') returns cloud provider when rule exists."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="calibration", provider="anthropic", model="claude-sonnet"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    calibration = router.for_task("calibration")
    assert calibration is not None
    assert calibration is not router.default

    from corpclaw_lite.llm.anthropic import AnthropicProvider

    assert isinstance(calibration, AnthropicProvider)


def test_vision_routing() -> None:
    """for_task('vision') returns dedicated provider for image analysis."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="vision", provider="ollama", model="glm-ocr"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    vision = router.for_task("vision")
    assert vision is not None
    assert vision is not router.default


def test_all_production_task_kinds_with_single_provider() -> None:
    """All production task_kinds work when pointing to same provider+model."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="vision", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="consolidate", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="compress", provider="ollama", model="qwen3.5-4b"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    # All should return the same cached instance (same provider+model+preset)
    default = router.for_task("default")
    assert router.for_task("vision") is default
    assert router.for_task("consolidate") is default
    assert router.for_task("compress") is default


def test_subagent_research_agent_routing() -> None:
    """for_subagent('research-agent') returns a dedicated provider."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(subagent_id="research-agent", provider="anthropic", model="claude-3-haiku"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    research = router.for_subagent("research-agent")
    assert research is not None
    assert research is not router.default

    from corpclaw_lite.llm.anthropic import AnthropicProvider

    assert isinstance(research, AnthropicProvider)


def test_for_subagent_preserves_run_id_on_queued_provider() -> None:
    """Queue/cache wrapper must receive the subagent run_id for trace correlation."""
    registry = _make_registry()
    settings = LLMSettings(
        routing=[
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(subagent_id="research-agent", provider="ollama", model="qwen3.5-4b"),
        ],
        max_concurrent_requests=1,
        queue={"enabled": True},
    )
    router = LLMRouter.from_settings(settings, registry)

    research = router.for_subagent("research-agent", user_id="user-1", run_id="sub-run")

    from corpclaw_lite.llm.router import QueuedProvider

    assert isinstance(research, QueuedProvider)
    assert research._run_id == "sub-run"  # type: ignore[attr-defined]
    assert research._agent_id == "research-agent"  # type: ignore[attr-defined]
    assert research._task_kind == "subagent:research-agent"  # type: ignore[attr-defined]


def test_unknown_task_kind_falls_back_gracefully() -> None:
    """Unknown task_kind always returns default, no crash."""
    registry = _make_registry()
    settings = _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="vision", provider="ollama", model="glm-ocr"),
        ]
    )
    router = LLMRouter.from_settings(settings, registry)

    # These have no routing rules — should all fall back to default
    assert router.for_task("consolidate") is router.default
    assert router.for_task("compress") is router.default
    assert router.for_task("calibration") is router.default
    assert router.for_task("nonexistent") is router.default


# ── with_overrides (D-056 PR3) ──────────────────────────────────────────────


def _agent_routes_settings() -> LLMSettings:
    """Settings with default + vision + compress + consolidate + a non-agent route."""
    return _make_settings(
        [
            RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="vision", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="compress", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="consolidate", provider="ollama", model="qwen3.5-4b"),
            RoutingRule(task_kind="eval", provider="anthropic", model="claude-3-haiku"),
        ]
    )


def _preset_registry_for_overrides() -> PresetRegistry:
    """A split-format registry with a model profile and two sampling profiles."""
    import textwrap
    from pathlib import Path

    yaml = textwrap.dedent("""\
        models:
          qwen3.5-4b:
            default_inference:
              temperature: 0.7
          gemma-alt:
            default_inference:
              temperature: 0.7
        sampling:
          base-default:
            model: qwen3.5-4b
            thinking_mode: default
          off-profile:
            model: qwen3.5-4b
            thinking_mode: off
    """)
    p = Path("/tmp/_test_overrides_presets.yaml")
    p.write_text(yaml, encoding="utf-8")
    return PresetRegistry.from_yaml(p)


def test_with_overrides_applies_to_all_agent_routes() -> None:
    """with_overrides(apply_to=all_agent_routes) overrides default/vision/compress/consolidate,
    leaving the non-agent 'eval' route untouched."""
    registry = _make_registry()
    settings = _agent_routes_settings()
    preset_reg = _preset_registry_for_overrides()
    router = LLMRouter.from_settings(settings, registry, preset_reg)

    overridden = router.with_overrides(
        provider_registry=registry,
        preset_registry=preset_reg,
        sampling_name="off-profile",
        apply_to="all_agent_routes",
    )

    # Queue is disabled in _make_settings → for_task returns the raw provider.
    # Agent-facing routes got new providers (different objects).
    assert overridden.for_task("default") is not router.for_task("default")
    assert overridden.for_task("vision") is not router.for_task("vision")
    # Non-agent route preserved (same underlying provider object).
    assert overridden.for_task("eval") is router.for_task("eval")


def test_with_overrides_default_only_preserves_others() -> None:
    """apply_to=default_only overrides just default; vision/compress preserved."""
    registry = _make_registry()
    settings = _agent_routes_settings()
    preset_reg = _preset_registry_for_overrides()
    router = LLMRouter.from_settings(settings, registry, preset_reg)

    overridden = router.with_overrides(
        provider_registry=registry,
        preset_registry=preset_reg,
        sampling_name="off-profile",
        apply_to="default_only",
    )

    assert overridden.for_task("default") is not router.for_task("default")
    # vision/compress/consolidate preserved.
    assert overridden.for_task("vision") is router.for_task("vision")
    assert overridden.for_task("compress") is router.for_task("compress")


def test_with_overrides_thinking_off_via_override() -> None:
    """thinking=ThinkingOverride(mode='off') produces an off SamplingProfile on default."""
    from corpclaw_lite.llm.base import ThinkingOverride

    registry = _make_registry()
    settings = _agent_routes_settings()
    preset_reg = _preset_registry_for_overrides()
    router = LLMRouter.from_settings(settings, registry, preset_reg)

    overridden = router.with_overrides(
        provider_registry=registry,
        preset_registry=preset_reg,
        thinking=ThinkingOverride(mode="off"),
        apply_to="default_only",
    )
    default_provider = overridden.for_task("default")
    assert default_provider._sampling is not None  # type: ignore[attr-defined]
    assert default_provider._sampling.thinking_mode == "off"  # type: ignore[attr-defined]


def test_with_overrides_model_swap() -> None:
    """model= override swaps the model and looks up its ModelProfile."""
    registry = _make_registry()
    settings = _agent_routes_settings()
    preset_reg = _preset_registry_for_overrides()
    router = LLMRouter.from_settings(settings, registry, preset_reg)

    overridden = router.with_overrides(
        provider_registry=registry,
        preset_registry=preset_reg,
        model="gemma-alt",
        apply_to="default_only",
    )
    default_provider = overridden.for_task("default")
    assert default_provider._model == "gemma-alt"  # type: ignore[attr-defined]
    # ModelProfile resolved by the new model name.
    assert default_provider._model_profile is not None  # type: ignore[attr-defined]


def test_with_overrides_shares_queue_and_cache() -> None:
    """with_overrides shares the parent's queue and cache_manager instances."""
    settings = LLMSettings(
        routing=[RoutingRule(task_kind="default", provider="ollama", model="qwen3.5-4b")],
        max_concurrent_requests=1,
        queue={"enabled": True},
    )
    registry = _make_registry()
    preset_reg = _preset_registry_for_overrides()
    router = LLMRouter.from_settings(settings, registry, preset_reg)

    overridden = router.with_overrides(
        provider_registry=registry,
        preset_registry=preset_reg,
        sampling_name="off-profile",
        apply_to="default_only",
    )
    assert overridden.queue is router.queue
    assert overridden._cache_manager is router._cache_manager  # type: ignore[attr-defined]


def test_with_overrides_provider_meta_populated() -> None:
    """provider_meta contains an entry for the new provider's id (cache-scope)."""
    registry = _make_registry()
    settings = _agent_routes_settings()
    preset_reg = _preset_registry_for_overrides()
    router = LLMRouter.from_settings(settings, registry, preset_reg)

    overridden = router.with_overrides(
        provider_registry=registry,
        preset_registry=preset_reg,
        sampling_name="off-profile",
        apply_to="default_only",
    )
    default_provider = overridden.for_task("default")
    assert id(default_provider) in overridden._provider_meta  # type: ignore[attr-defined]
    meta = overridden._provider_meta[id(default_provider)]  # type: ignore[attr-defined]
    assert meta[1] == "qwen3.5-4b"  # model preserved
    assert meta[2] == "off-profile"  # profile label = override sampling name


def test_with_overrides_unknown_sampling_name_falls_back() -> None:
    """Unknown sampling_name logs a warning and keeps the route's existing profile."""
    registry = _make_registry()
    settings = _agent_routes_settings()
    preset_reg = _preset_registry_for_overrides()
    router = LLMRouter.from_settings(settings, registry, preset_reg)

    overridden = router.with_overrides(
        provider_registry=registry,
        preset_registry=preset_reg,
        sampling_name="does-not-exist",
        apply_to="default_only",
    )
    # Override still applied (route rebuilt), but sampling falls back to None-derived.
    default_provider = overridden.for_task("default")
    # No crash; the route got a new provider.
    assert default_provider is not router.for_task("default")


def test_with_overrides_default_thinking_is_noop() -> None:
    """thinking=ThinkingOverride(mode='default') does not change the sampling profile."""
    from corpclaw_lite.llm.base import ThinkingOverride

    registry = _make_registry()
    settings = _agent_routes_settings()
    preset_reg = _preset_registry_for_overrides()
    router = LLMRouter.from_settings(settings, registry, preset_reg)

    overridden = router.with_overrides(
        provider_registry=registry,
        preset_registry=preset_reg,
        thinking=ThinkingOverride(mode="default"),
        apply_to="default_only",
    )
    default_provider = overridden.for_task("default")
    # 'default' thinking = no override → sampling carries base profile's mode.
    if default_provider._sampling is not None:  # type: ignore[attr-defined]
        assert default_provider._sampling.thinking_mode == "default"  # type: ignore[attr-defined]
