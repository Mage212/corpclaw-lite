"""Tests for per-call RequestOptions (D-056).

Covers:
  - Two independent contextvars (backend extra_body vs per-call options)
  - ThinkingOverride modes (off, budget, default)
  - Merge priority in OpenAIProvider._build_chat_kwargs:
      ModelProfile.default_inference (lowest)
        < SamplingProfile.inference_overrides + thinking_mode
          < RequestOptions.inference / RequestOptions.thinking (per-call, highest)
            < BackendRequestOptions.extra_body (transport)
  - Legacy preset bridge still works end-to-end
"""

from __future__ import annotations

from typing import Any

from corpclaw_lite.config.providers import ProviderSettings
from corpclaw_lite.llm.base import (
    BackendRequestOptions,
    RequestOptions,
    ThinkingOverride,
    get_request_options,
    reset_backend_request_options,
    reset_request_options,
    set_backend_request_options,
    set_request_options,
)
from corpclaw_lite.llm.openai import OpenAIProvider
from corpclaw_lite.llm.presets import ModelProfile, SamplingProfile, ThinkingConfig

_BASE_URL = "http://localhost:1234/v1"
_SETTINGS = ProviderSettings(model="test", api_key="key", base_url=_BASE_URL)


def _provider(
    model_profile: ModelProfile | None = None,
    sampling: SamplingProfile | None = None,
) -> OpenAIProvider:
    return OpenAIProvider(_SETTINGS, model_profile=model_profile, sampling=sampling)


# ── Contextvar helpers ─────────────────────────────────────────────────────────


def test_request_options_default_is_none() -> None:
    assert get_request_options() is None


def test_request_options_set_reset_roundtrip() -> None:
    opts = RequestOptions(thinking=ThinkingOverride(mode="off"))
    assert get_request_options() is None
    token = set_request_options(opts)
    assert get_request_options() is opts
    reset_request_options(token)
    assert get_request_options() is None


def test_request_options_independent_of_backend_options() -> None:
    """The two contextvars are independent — setting one doesn't touch the other."""
    call_opts = RequestOptions(inference={"temperature": 0.1})
    backend_opts = BackendRequestOptions(extra_body={"id_slot": 2})

    call_tok = set_request_options(call_opts)
    backend_tok = set_backend_request_options(backend_opts)
    try:
        assert get_request_options() is call_opts
        # Backend options unchanged by per-call set.
        assert get_request_options() is call_opts
    finally:
        reset_request_options(call_tok)
        reset_backend_request_options(backend_tok)


def test_request_options_nested_context() -> None:
    """Inner set overrides outer; reset restores outer."""
    outer = RequestOptions(inference={"temperature": 0.2})
    inner = RequestOptions(inference={"temperature": 0.9})

    outer_tok = set_request_options(outer)
    assert get_request_options() is outer
    inner_tok = set_request_options(inner)
    assert get_request_options() is inner
    reset_request_options(inner_tok)
    assert get_request_options() is outer
    reset_request_options(outer_tok)
    assert get_request_options() is None


# ── ThinkingOverride ───────────────────────────────────────────────────────────


def test_thinking_override_default_is_noop_in_build() -> None:
    """mode='default' does not inject chat_template_kwargs or max_tokens."""
    provider = _provider(sampling=SamplingProfile(thinking_mode="off"))
    kwargs: dict[str, Any] = {}
    # Apply sampling first (sets off), then per-call default override.
    provider._apply_sampling(kwargs)
    assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False

    # Per-call default override should NOT flip it back on or remove it
    # (default = "leave alone").
    tok = set_request_options(RequestOptions(thinking=ThinkingOverride(mode="default")))
    try:
        provider._apply_request_options(kwargs)
    finally:
        reset_request_options(tok)
    # Sampling's "off" stands because per-call is "default" (no-op).
    assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_thinking_override_off_injects_enable_thinking_false() -> None:
    provider = _provider()
    kwargs: dict[str, Any] = {}
    tok = set_request_options(RequestOptions(thinking=ThinkingOverride(mode="off")))
    try:
        provider._apply_request_options(kwargs)
    finally:
        reset_request_options(tok)
    assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_thinking_override_budget_caps_max_tokens() -> None:
    provider = _provider()
    kwargs: dict[str, Any] = {}
    tok = set_request_options(RequestOptions(thinking=ThinkingOverride(mode="budget", budget=256)))
    try:
        provider._apply_request_options(kwargs)
    finally:
        reset_request_options(tok)
    assert kwargs["max_tokens"] == 256 + 1024


def test_thinking_override_budget_without_budget_is_noop() -> None:
    """mode='budget' with no budget value does not set max_tokens."""
    provider = _provider()
    kwargs: dict[str, Any] = {}
    tok = set_request_options(RequestOptions(thinking=ThinkingOverride(mode="budget")))
    try:
        provider._apply_request_options(kwargs)
    finally:
        reset_request_options(tok)
    assert "max_tokens" not in kwargs


# ── Merge priority ─────────────────────────────────────────────────────────────


def test_model_profile_default_inference_applied() -> None:
    """ModelProfile.default_inference provides the lowest layer."""
    provider = _provider(
        model_profile=ModelProfile(default_inference={"temperature": 0.4, "top_k": 20})
    )
    kwargs: dict[str, Any] = {}
    provider._apply_model_profile(None, kwargs)
    assert kwargs["temperature"] == 0.4
    # top_k is non-standard → routed to extra_body.
    assert "top_k" not in kwargs
    assert kwargs["extra_body"]["top_k"] == 20


def test_sampling_overrides_model_profile() -> None:
    """SamplingProfile.inference_overrides win over ModelProfile defaults."""
    provider = _provider(
        model_profile=ModelProfile(default_inference={"temperature": 0.4}),
        sampling=SamplingProfile(inference_overrides={"temperature": 0.8}),
    )
    kwargs: dict[str, Any] = {}
    provider._apply_model_profile(None, kwargs)
    provider._apply_sampling(kwargs)
    assert kwargs["temperature"] == 0.8


def test_request_options_override_sampling() -> None:
    """RequestOptions.inference (per-call) win over SamplingProfile."""
    provider = _provider(
        model_profile=ModelProfile(default_inference={"temperature": 0.4}),
        sampling=SamplingProfile(inference_overrides={"temperature": 0.8}),
    )
    kwargs: dict[str, Any] = {}
    provider._apply_model_profile(None, kwargs)
    provider._apply_sampling(kwargs)
    tok = set_request_options(RequestOptions(inference={"temperature": 0.1}))
    try:
        provider._apply_request_options(kwargs)
    finally:
        reset_request_options(tok)
    assert kwargs["temperature"] == 0.1


def test_request_options_thinking_off_overrides_sampling() -> None:
    """Per-call thinking off wins over sampling's thinking_mode."""
    provider = _provider(
        sampling=SamplingProfile(thinking_mode="default"),
    )
    kwargs: dict[str, Any] = {}
    provider._apply_sampling(kwargs)
    tok = set_request_options(RequestOptions(thinking=ThinkingOverride(mode="off")))
    try:
        provider._apply_request_options(kwargs)
    finally:
        reset_request_options(tok)
    assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_request_options_non_standard_param_to_extra_body() -> None:
    """Non-standard inference overrides go to extra_body, winning over sampling."""
    provider = _provider(
        model_profile=ModelProfile(default_inference={"top_k": 20}),
    )
    kwargs: dict[str, Any] = {}
    provider._apply_model_profile(None, kwargs)
    assert kwargs["extra_body"]["top_k"] == 20
    tok = set_request_options(RequestOptions(inference={"top_k": 64}))
    try:
        provider._apply_request_options(kwargs)
    finally:
        reset_request_options(tok)
    assert kwargs["extra_body"]["top_k"] == 64


# ── End-to-end via _build_chat_kwargs ─────────────────────────────────────────


def test_build_chat_kwargs_full_merge_priority() -> None:
    """Full merge: profile < sampling < per-call, with system_prompt_prefix."""
    provider = _provider(
        model_profile=ModelProfile(
            system_prompt_prefix="<|think|>",
            default_inference={"temperature": 0.4, "top_k": 20},
        ),
        sampling=SamplingProfile(
            inference_overrides={"temperature": 0.8},
            thinking_mode="off",
        ),
    )
    tok = set_request_options(RequestOptions(inference={"temperature": 0.1}))
    try:
        kwargs, final_messages = provider._build_chat_kwargs(
            [{"role": "user", "content": "hi"}],
            system="You are helpful.",
        )
    finally:
        reset_request_options(tok)

    # Per-call temperature wins.
    assert kwargs["temperature"] == 0.1
    # Model profile default top_k present in extra_body.
    assert kwargs["extra_body"]["top_k"] == 20
    # Sampling thinking off injected.
    assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    # system_prompt_prefix prepended (model-profile layer).
    assert final_messages[0]["content"] == "<|think|>\nYou are helpful."


def test_build_chat_kwargs_no_profiles_no_options_minimal() -> None:
    """Bare provider with no profiles/options builds minimal kwargs."""
    provider = _provider()
    kwargs, final_messages = provider._build_chat_kwargs(
        [{"role": "user", "content": "hi"}],
    )
    assert kwargs["model"] == "test"
    assert "temperature" not in kwargs
    assert final_messages == [{"role": "user", "content": "hi"}]


def test_build_chat_kwargs_backend_extra_body_merged() -> None:
    """BackendRequestOptions.extra_body is merged last (transport layer)."""
    provider = _provider(
        model_profile=ModelProfile(default_inference={"top_k": 20}),
    )
    backend_tok = set_backend_request_options(
        BackendRequestOptions(extra_body={"id_slot": 2, "cache_prompt": True})
    )
    try:
        kwargs, _ = provider._build_chat_kwargs([{"role": "user", "content": "hi"}])
    finally:
        reset_backend_request_options(backend_tok)

    # Model-profile top_k and backend id_slot/cache_prompt coexist in extra_body.
    assert kwargs["extra_body"]["top_k"] == 20
    assert kwargs["extra_body"]["id_slot"] == 2
    assert kwargs["extra_body"]["cache_prompt"] is True


# ── Legacy preset bridge end-to-end ────────────────────────────────────────────


def test_legacy_preset_bridge_produces_equivalent_profiles() -> None:
    """A legacy ModelPreset bridges to (ModelProfile, SamplingProfile) preserving semantics."""
    from corpclaw_lite.llm.presets import ModelPreset, profile_from_legacy_preset

    preset = ModelPreset(
        description="legacy",
        system_prompt_prefix="<|think|>",
        thinking=ThinkingConfig(source="native"),
        thinking_budget_tokens=512,
        inference_params={"temperature": 0.7, "top_k": 20},
    )
    mp, sp = profile_from_legacy_preset(preset)
    assert mp.system_prompt_prefix == "<|think|>"
    assert mp.thinking_parser is not None
    assert mp.thinking_parser.source == "native"
    assert mp.default_inference == {"temperature": 0.7, "top_k": 20}
    assert sp.thinking_mode == "budget"
    assert sp.thinking_budget == 512


def test_provider_accepts_legacy_preset_kwarg() -> None:
    """OpenAIProvider(settings, preset=...) still works (back-compat)."""
    from corpclaw_lite.llm.presets import ModelPreset

    preset = ModelPreset(inference_params={"temperature": 0.9}, thinking_budget_tokens=128)
    provider = OpenAIProvider(_SETTINGS, preset=preset)
    # Internally bridged to profiles.
    assert provider._model_profile is not None
    assert provider._model_profile.default_inference["temperature"] == 0.9
    assert provider._sampling is not None
    assert provider._sampling.thinking_mode == "budget"
    assert provider._sampling.thinking_budget == 128
    # Legacy field kept for introspection.
    assert provider._preset is preset


def test_build_provider_passes_profiles() -> None:
    """build_provider accepts model_profile/sampling (new-style) directly."""
    from corpclaw_lite.config.providers import ProviderConnection
    from corpclaw_lite.llm.router import build_provider

    conn = ProviderConnection(type="openai", api_key="dummy", base_url=_BASE_URL)
    mp = ModelProfile(default_inference={"temperature": 0.3})
    sp = SamplingProfile(thinking_mode="off")
    provider = build_provider(conn, model="m", model_profile=mp, sampling=sp)
    assert provider is not None
    assert provider._model_profile is mp  # type: ignore[attr-defined]
    assert provider._sampling is sp  # type: ignore[attr-defined]
