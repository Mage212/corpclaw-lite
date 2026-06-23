"""Tests for the model presets system.

Covers:
  - YAML loading (valid, empty, missing)
  - Preset lookup (found, not found)
  - Inference params merging (request > preset > defaults)
  - System prompt prefix injection
  - Thinking budget cap
  - Reasoning parsing (content tags, native field, no preset)
  - LLMResponse reasoning field
  - Reasoning stored in SQLiteMemory
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from corpclaw_lite.llm.base import LLMResponse
from corpclaw_lite.llm.presets import ModelPreset, PresetRegistry, ThinkingConfig

# ── PresetRegistry loading ────────────────────────────────────────────────────


def test_load_presets_yaml(tmp_path: Path) -> None:
    """Valid YAML with multiple presets is loaded correctly."""
    yaml_content = textwrap.dedent("""\
        presets:
          gemma4-thinking:
            description: "Gemma 4 thinking"
            system_prompt_prefix: "<|think|>"
            thinking:
              open_tag: "<|channel>thought"
              close_tag: "<channel|>"
              source: "content"
            inference_params:
              temperature: 1.0
              top_p: 0.95

          fast-mode:
            description: "No thinking"
            inference_params:
              temperature: 0.3
    """)
    f = tmp_path / "presets.yaml"
    f.write_text(yaml_content, encoding="utf-8")

    registry = PresetRegistry.from_yaml(f)
    assert registry.list_all() == ["gemma4-thinking", "fast-mode"]

    preset = registry.get("gemma4-thinking")
    assert preset is not None
    assert preset.system_prompt_prefix == "<|think|>"
    assert preset.thinking is not None
    assert preset.thinking.source == "content"
    assert preset.thinking.open_tag == "<|channel>thought"
    assert preset.inference_params["temperature"] == 1.0


def test_load_empty_yaml(tmp_path: Path) -> None:
    """Empty YAML → empty registry."""
    f = tmp_path / "empty.yaml"
    f.write_text("", encoding="utf-8")
    registry = PresetRegistry.from_yaml(f)
    assert registry.list_all() == []


def test_load_missing_file(tmp_path: Path) -> None:
    """Missing file → empty registry, no crash."""
    registry = PresetRegistry.from_yaml(tmp_path / "nope.yaml")
    assert registry.list_all() == []


def test_get_unknown_preset() -> None:
    """Unknown preset name returns None."""
    registry = PresetRegistry()
    assert registry.get("nonexistent") is None


def test_invalid_preset_skipped(tmp_path: Path) -> None:
    """Invalid preset definition is skipped, others still load."""
    yaml_content = textwrap.dedent("""\
        presets:
          good:
            description: "Valid preset"
          bad:
            thinking:
              source: "invalid_source"
    """)
    f = tmp_path / "presets.yaml"
    f.write_text(yaml_content, encoding="utf-8")
    registry = PresetRegistry.from_yaml(f)
    assert "good" in registry.list_all()
    # "bad" has invalid source enum → skipped
    assert registry.get("bad") is None


# ── Inference params merging ──────────────────────────────────────────────────


def test_inference_params_merge_request_priority() -> None:
    """Request-level params take priority over preset params.

    Standard OpenAI params (temperature, top_p) land in kwargs directly.
    Non-standard params (top_k) are routed to kwargs["extra_body"] so the
    OpenAI SDK doesn't reject them client-side.
    """
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    preset = ModelPreset(inference_params={"temperature": 1.0, "top_p": 0.95, "top_k": 64})
    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
        preset=preset,
    )

    # Simulate kwargs with request-level temperature
    kwargs: dict[str, Any] = {"temperature": 0.3}
    provider._apply_preset(None, kwargs)

    assert kwargs["temperature"] == 0.3  # request-level wins
    assert kwargs["top_p"] == 0.95  # standard param → in kwargs
    # top_k is non-standard → routed to extra_body, not top-level kwargs
    assert "top_k" not in kwargs
    assert kwargs["extra_body"]["top_k"] == 64


def test_inference_params_merge_no_preset() -> None:
    """Without preset, kwargs are unchanged."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
    )
    kwargs: dict[str, Any] = {"temperature": 0.5}
    result = provider._apply_preset("You are helpful", kwargs)
    assert result == "You are helpful"
    assert kwargs == {"temperature": 0.5}


# ── System prompt prefix ─────────────────────────────────────────────────────


def test_system_prompt_injection() -> None:
    """system_prompt_prefix is prepended to existing system prompt."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    preset = ModelPreset(system_prompt_prefix="<|think|>")
    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
        preset=preset,
    )
    result = provider._apply_preset("You are helpful.", {})
    assert result == "<|think|>\nYou are helpful."


def test_system_prompt_injection_no_existing() -> None:
    """system_prompt_prefix works when system is None."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    preset = ModelPreset(system_prompt_prefix="<|think|>")
    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
        preset=preset,
    )
    result = provider._apply_preset(None, {})
    assert result == "<|think|>"


# ── Thinking budget ───────────────────────────────────────────────────────────


def test_thinking_budget_caps_max_tokens() -> None:
    """thinking_budget_tokens sets max_tokens = budget + 1024."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    preset = ModelPreset(thinking_budget_tokens=512)
    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
        preset=preset,
    )
    kwargs: dict[str, Any] = {}
    provider._apply_preset(None, kwargs)
    assert kwargs["max_tokens"] == 512 + 1024


def test_thinking_budget_does_not_override_explicit() -> None:
    """Explicit max_tokens in request is not overridden by thinking budget."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    preset = ModelPreset(thinking_budget_tokens=512)
    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
        preset=preset,
    )
    kwargs: dict[str, Any] = {"max_tokens": 200}
    provider._apply_preset(None, kwargs)
    assert kwargs["max_tokens"] == 200  # request wins


# ── Reasoning parsing ────────────────────────────────────────────────────────


def test_parse_reasoning_content_tags() -> None:
    """Gemma4-style: reasoning extracted from content tags."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    preset = ModelPreset(
        thinking=ThinkingConfig(
            open_tag="<|channel>thought",
            close_tag="<channel|>",
            source="content",
        )
    )
    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
        preset=preset,
    )

    msg = MagicMock()
    msg.content = "<|channel>thought\nI think 2+2=4\n<channel|>The answer is 4."

    reasoning, content = provider._parse_reasoning(msg)
    assert reasoning == "I think 2+2=4"
    assert content == "The answer is 4."


def test_parse_reasoning_native_field() -> None:
    """Qwen3-style: reasoning from native reasoning_content field."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    preset = ModelPreset(thinking=ThinkingConfig(source="native"))
    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
        preset=preset,
    )

    msg = MagicMock()
    msg.content = "The answer is 4."
    msg.reasoning_content = "Step 1: 2+2=4"

    reasoning, content = provider._parse_reasoning(msg)
    assert reasoning == "Step 1: 2+2=4"
    assert content == "The answer is 4."


def test_parse_reasoning_no_preset() -> None:
    """Without preset, content is returned as-is, no reasoning."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
    )

    msg = MagicMock()
    msg.content = "<think>some thinking</think>The answer"

    reasoning, content = provider._parse_reasoning(msg)
    assert reasoning == ""
    # Without preset, raw content is returned as-is (no stripping)
    assert content == "<think>some thinking</think>The answer"


def test_parse_reasoning_no_tags_found() -> None:
    """With content-tag preset but no tags in output, returns raw content."""
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    preset = ModelPreset(
        thinking=ThinkingConfig(
            open_tag="<|channel>thought",
            close_tag="<channel|>",
            source="content",
        )
    )
    provider = OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
        preset=preset,
    )

    msg = MagicMock()
    msg.content = "Just a plain answer without thinking."

    reasoning, content = provider._parse_reasoning(msg)
    assert reasoning == ""
    assert content == "Just a plain answer without thinking."


# ── LLMResponse reasoning field ──────────────────────────────────────────────


def test_llm_response_reasoning_field() -> None:
    """LLMResponse has a reasoning field, default empty."""
    resp = LLMResponse(content="hello")
    assert resp.reasoning == ""

    resp2 = LLMResponse(content="hello", reasoning="I thought about it")
    assert resp2.reasoning == "I thought about it"


# ── Memory reasoning storage ─────────────────────────────────────────────────


@pytest.mark.anyio()
async def test_reasoning_stored_in_memory(tmp_path: Path) -> None:
    """add_message stores reasoning in the database."""
    import sqlite3

    from corpclaw_lite.memory.sqlite import SQLiteMemory

    db_path = tmp_path / "test_memory.db"

    # Patch data dir for test
    import corpclaw_lite.memory.sqlite as mem_module

    original_data_dir = mem_module._DATA_DIR
    mem_module._DATA_DIR = tmp_path
    try:
        memory = SQLiteMemory(db_path=db_path.name)
        await memory.add_message(
            "user1",
            "assistant",
            "The answer is 4.",
            reasoning="Step 1: 2+2=4",
        )

        # Verify reasoning is in the database
        with sqlite3.connect(str(memory.db_path)) as conn:
            row = conn.execute(
                "SELECT content, reasoning FROM messages WHERE user_id = ?", ("user1",)
            ).fetchone()
        assert row is not None
        assert row[0] == "The answer is 4."
        assert row[1] == "Step 1: 2+2=4"

        # get_history does NOT return reasoning
        history = await memory.get_history("user1")
        assert len(history) == 1
        assert history[0]["content"] == "The answer is 4."
        assert "reasoning" not in history[0]
    finally:
        mem_module._DATA_DIR = original_data_dir


@pytest.mark.anyio()
async def test_add_message_without_reasoning(tmp_path: Path) -> None:
    """add_message works without reasoning (backward compatible)."""
    import sqlite3

    import corpclaw_lite.memory.sqlite as mem_module
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    original_data_dir = mem_module._DATA_DIR
    mem_module._DATA_DIR = tmp_path
    try:
        memory = SQLiteMemory(db_path="test_compat.db")
        await memory.add_message("user1", "user", "hello")

        with sqlite3.connect(str(memory.db_path)) as conn:
            row = conn.execute(
                "SELECT content, reasoning FROM messages WHERE user_id = ?", ("user1",)
            ).fetchone()
        assert row is not None
        assert row[0] == "hello"
        assert row[1] is None
    finally:
        mem_module._DATA_DIR = original_data_dir


# ── Router integration ────────────────────────────────────────────────────────


def test_build_provider_with_preset() -> None:
    """build_provider passes preset to OpenAIProvider."""
    from corpclaw_lite.config.providers import ProviderConnection
    from corpclaw_lite.llm.router import build_provider

    preset = ModelPreset(inference_params={"temperature": 0.9})
    conn = ProviderConnection(
        type="openai",
        api_key="dummy",
        base_url="http://localhost:1234/v1",
    )
    provider = build_provider(conn, model="test-model", preset=preset)
    assert provider is not None
    assert provider._preset is preset  # type: ignore[attr-defined]


def test_build_provider_without_preset() -> None:
    """build_provider works without preset — preset is None."""
    from corpclaw_lite.config.providers import ProviderConnection
    from corpclaw_lite.llm.router import build_provider

    conn = ProviderConnection(
        type="openai",
        api_key="dummy",
        base_url="http://localhost:1234/v1",
    )
    provider = build_provider(conn, model="test-model")
    assert provider is not None
    assert provider._preset is None  # type: ignore[attr-defined]


def test_build_provider_anthropic_requires_key() -> None:
    """build_provider returns None for Anthropic without api_key."""
    from corpclaw_lite.config.providers import ProviderConnection
    from corpclaw_lite.llm.router import build_provider

    conn = ProviderConnection(type="anthropic")
    provider = build_provider(conn, model="claude-3-haiku-20240307")
    assert provider is None


# ── Split format (D-056): ModelProfile / SamplingProfile ─────────────────────


def test_load_split_format_yaml(tmp_path: Path) -> None:
    """New models:/sampling: format loads into separate registries."""
    from corpclaw_lite.llm.presets import PresetRegistry

    yaml_content = textwrap.dedent("""\
        models:
          gemma4:
            thinking_parser:
              open_tag: "<|channel>thought"
              close_tag: "<channel|>"
              source: "content"
            system_prompt_prefix: "<|think|>"
            default_inference:
              temperature: 0.7
              top_k: 64

        sampling:
          gemma4-default:
            model: gemma4
            thinking_mode: default
            inference_overrides:
              temperature: 1.0
          gemma4-fast:
            model: gemma4
            thinking_mode: off
          aux-no-thinking:
            model: gemma4
            thinking_mode: off
            inference_overrides:
              temperature: 0.2
    """)
    f = tmp_path / "presets.yaml"
    f.write_text(yaml_content, encoding="utf-8")

    registry = PresetRegistry.from_yaml(f)
    assert registry.list_model_profiles() == ["gemma4"]
    assert set(registry.list_sampling_profiles()) == {
        "gemma4-default",
        "gemma4-fast",
        "aux-no-thinking",
    }

    mp = registry.get_model_profile("gemma4")
    assert mp is not None
    assert mp.system_prompt_prefix == "<|think|>"
    assert mp.thinking_parser is not None
    assert mp.thinking_parser.source == "content"
    assert mp.default_inference["temperature"] == 0.7

    sp = registry.get_sampling_profile("gemma4-fast")
    assert sp is not None
    assert sp.model == "gemma4"
    assert sp.thinking_mode == "off"

    sp_aux = registry.get_sampling_profile("aux-no-thinking")
    assert sp_aux is not None
    assert sp_aux.inference_overrides["temperature"] == 0.2


def test_load_legacy_and_split_mixed(tmp_path: Path) -> None:
    """Legacy presets: and new models:/sampling: coexist in one file."""
    from corpclaw_lite.llm.presets import PresetRegistry

    yaml_content = textwrap.dedent("""\
        models:
          qwen3:
            default_inference:
              temperature: 0.7

        sampling:
          qwen3-default:
            model: qwen3

        presets:
          legacy-foo:
            inference_params:
              temperature: 0.5
    """)
    f = tmp_path / "presets.yaml"
    f.write_text(yaml_content, encoding="utf-8")

    registry = PresetRegistry.from_yaml(f)
    # New-style entries.
    assert registry.get_model_profile("qwen3") is not None
    assert registry.get_sampling_profile("qwen3-default") is not None
    # Legacy entry split into virtual (ModelProfile, SamplingProfile) pair.
    assert registry.get_model_profile("legacy-foo") is not None
    assert registry.get_sampling_profile("legacy-foo") is not None


def test_legacy_get_back_compat_returns_combined_preset(tmp_path: Path) -> None:
    """registry.get(name) (legacy) reconstructs a combined ModelPreset from a profile."""
    from corpclaw_lite.llm.presets import PresetRegistry

    yaml_content = textwrap.dedent("""\
        models:
          gemma4:
            system_prompt_prefix: "<|think|>"
            default_inference:
              temperature: 0.7
    """)
    f = tmp_path / "presets.yaml"
    f.write_text(yaml_content, encoding="utf-8")

    registry = PresetRegistry.from_yaml(f)
    combined = registry.get("gemma4")
    assert combined is not None
    assert combined.system_prompt_prefix == "<|think|>"
    assert combined.inference_params["temperature"] == 0.7


def test_list_all_union_of_profiles(tmp_path: Path) -> None:
    """list_all() (legacy) returns the union of model + sampling names."""
    from corpclaw_lite.llm.presets import PresetRegistry

    yaml_content = textwrap.dedent("""\
        models:
          m1: {}
          m2: {}
        sampling:
          s1: {model: m1}
    """)
    f = tmp_path / "presets.yaml"
    f.write_text(yaml_content, encoding="utf-8")

    registry = PresetRegistry.from_yaml(f)
    names = set(registry.list_all())
    assert names == {"m1", "m2", "s1"}


def test_invalid_model_profile_skipped(tmp_path: Path) -> None:
    """Invalid model profile (bad enum) is skipped, others load."""
    from corpclaw_lite.llm.presets import PresetRegistry

    yaml_content = textwrap.dedent("""\
        models:
          good:
            default_inference:
              temperature: 0.7
          bad:
            thinking_parser:
              source: "bogus"
    """)
    f = tmp_path / "presets.yaml"
    f.write_text(yaml_content, encoding="utf-8")

    registry = PresetRegistry.from_yaml(f)
    assert registry.get_model_profile("good") is not None
    assert registry.get_model_profile("bad") is None


def test_model_profile_to_preset_roundtrip() -> None:
    """ModelProfile.to_preset() reconstructs a combined preset (back-compat)."""
    from corpclaw_lite.llm.presets import ModelProfile, ThinkingConfig

    mp = ModelProfile(
        description="d",
        thinking_parser=ThinkingConfig(source="native"),
        system_prompt_prefix="P",
        default_inference={"temperature": 0.7},
    )
    preset = mp.to_preset()
    assert preset.description == "d"
    assert preset.thinking is not None
    assert preset.thinking.source == "native"
    assert preset.system_prompt_prefix == "P"
    assert preset.inference_params["temperature"] == 0.7


# ── Split-apply methods (OpenAIProvider) ─────────────────────────────────────


def _openai_with_profiles(model_profile: Any | None = None, sampling: Any | None = None) -> Any:
    from corpclaw_lite.config.providers import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    return OpenAIProvider(
        ProviderSettings(model="test", api_key="key", base_url="http://localhost:1234/v1"),
        model_profile=model_profile,
        sampling=sampling,
    )


def test_apply_model_profile_injects_prefix_and_defaults() -> None:
    from corpclaw_lite.llm.presets import ModelProfile

    provider = _openai_with_profiles(
        ModelProfile(
            system_prompt_prefix="<|think|>",
            default_inference={"temperature": 0.7, "top_k": 64},
        )
    )
    kwargs: dict[str, Any] = {}
    system = provider._apply_model_profile("You are helpful.", kwargs)
    assert system == "<|think|>\nYou are helpful."
    assert kwargs["temperature"] == 0.7
    assert kwargs["extra_body"]["top_k"] == 64


def test_apply_sampling_thinking_off_injects_chat_template_kwargs() -> None:
    from corpclaw_lite.llm.presets import SamplingProfile

    provider = _openai_with_profiles(sampling=SamplingProfile(thinking_mode="off"))
    kwargs: dict[str, Any] = {}
    provider._apply_sampling(kwargs)
    assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_apply_sampling_budget_caps_max_tokens() -> None:
    from corpclaw_lite.llm.presets import SamplingProfile

    provider = _openai_with_profiles(
        sampling=SamplingProfile(thinking_mode="budget", thinking_budget=256)
    )
    kwargs: dict[str, Any] = {}
    provider._apply_sampling(kwargs)
    assert kwargs["max_tokens"] == 256 + 1024


def test_apply_sampling_default_is_noop() -> None:
    from corpclaw_lite.llm.presets import SamplingProfile

    provider = _openai_with_profiles(sampling=SamplingProfile(thinking_mode="default"))
    kwargs: dict[str, Any] = {}
    provider._apply_sampling(kwargs)
    assert "extra_body" not in kwargs
    assert "max_tokens" not in kwargs


def test_parse_reasoning_uses_model_profile() -> None:
    """_parse_reasoning reads ModelProfile.thinking_parser (new API)."""
    from corpclaw_lite.llm.presets import ModelProfile, ThinkingConfig

    provider = _openai_with_profiles(
        ModelProfile(
            thinking_parser=ThinkingConfig(
                open_tag="<|channel>thought",
                close_tag="<channel|>",
                source="content",
            )
        )
    )
    msg = MagicMock()
    msg.content = "<|channel>thought\nI think 2+2=4\n<channel|>The answer is 4."
    reasoning, content = provider._parse_reasoning(msg)
    assert reasoning == "I think 2+2=4"
    assert content == "The answer is 4."
