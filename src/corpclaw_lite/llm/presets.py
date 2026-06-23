# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Model profiles + sampling profiles — named inference/thinking configuration.

Split (D-056) of the legacy combined ``ModelPreset`` into two orthogonal layers:

  :class:`ModelProfile`  — properties of a *model*, rarely change, bound to a
                           model id:
                             - thinking_parser: how reasoning is extracted
                               (``<think>`` tags vs ``reasoning_content`` field)
                             - system_prompt_prefix: injected before system
                               prompt (e.g. gemma4 ``<|think|>``)
                             - default_inference: model's default sampling
                               params (temperature/top_p/top_k/...)

  :class:`SamplingProfile` — properties of a *task / phase*, change freely,
                             references a ModelProfile by name:
                             - thinking_mode: default | off | budget
                             - thinking_budget: cap (meaningful with mode="budget")
                             - inference_overrides: per-task overrides on top of
                               the model's default_inference

This split removes duplicate presets (e.g. the old ``gemma4-thinking`` and
``gemma4-fast`` — one model, differing only in temperature/thinking — collapse
into one ``gemma4-26b-qat`` ModelProfile + two SamplingProfiles) and makes
per-call override orthogonal to per-model config.

YAML format (``config/model_presets.yaml``)::

    models:
      qwen3.6-35b-a3b:
        thinking_parser: {source: native}
        default_inference: {temperature: 0.7, top_p: 0.95, top_k: 20}

    sampling:
      qwen3.6-default: {model: qwen3.6-35b-a3b, thinking_mode: default}
      aux-no-thinking:
        model: qwen3.6-35b-a3b
        thinking_mode: off
        inference_overrides: {temperature: 0.2}

Legacy combined format is still supported (back-compat for overlays)::

    presets:
      qwen3.5-thinking:        # → virtual (ModelProfile "qwen3.5-thinking",
        thinking: {source: native}   #   SamplingProfile "qwen3.5-thinking")
        inference_params: {...}

Priority at the provider (``_build_chat_kwargs``):
    model_profile.default_inference < sampling.inference_overrides
        < RequestOptions.inference / RequestOptions.thinking (per-call)
            < BackendRequestOptions.extra_body (transport)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, field_validator

__all__ = [
    "ModelPreset",
    "ModelProfile",
    "PresetRegistry",
    "SamplingProfile",
    "ThinkingConfig",
    "profile_from_legacy_preset",
]

logger = logging.getLogger(__name__)


def _coerce_yaml_bool_thinking_mode(value: Any) -> str:
    """Coerce YAML-bool forms of thinking_mode into the string literal.

    YAML 1.1 parses the unquoted scalars ``off``/``on``/``yes``/``no`` (and
    ``true``/``false``) as booleans. So an operator writing
    ``thinking_mode: off`` gets ``False``, and ``thinking_mode: on`` gets
    ``True`` — both of which Pydantic's ``Literal`` would reject. This maps the
    common intended forms back to the canonical string literals::

        False / "off"  → "off"
        True  / "on"   → "default"   (on = natural thinking)
        None           → "default"
        str            → passthrough (validated against the Literal)
    """
    if value is False or value == "off":
        return "off"
    if value is True or value in ("on", "yes"):
        return "default"
    if value is None:
        return "default"
    return str(value)


class ThinkingConfig(BaseModel):
    """Configuration for how to extract reasoning from model output.

    Attributes:
        open_tag:  Opening delimiter for reasoning in the ``content`` field.
        close_tag: Closing delimiter for reasoning in the ``content`` field.
        source:    ``"content"`` — parse tags from content (Gemma 4 style).
                   ``"native"``  — read ``reasoning_content`` field (Qwen 3 style).
    """

    open_tag: str = "<think>"
    close_tag: str = "</think>"
    source: Literal["content", "native"] = "content"


class ModelProfile(BaseModel):
    """Properties of a specific model (rarely changes, bound to a model id).

    Corresponds to the model-bound half of the old combined :class:`ModelPreset`:
    thinking parser, system-prompt prefix, and the model's default sampling
    params. These travel with the model, not with the task.
    """

    description: str = ""
    thinking_parser: ThinkingConfig | None = None
    system_prompt_prefix: str | None = None
    default_inference: dict[str, Any] = {}

    def to_preset(self) -> ModelPreset:
        """Reconstruct a legacy combined :class:`ModelPreset` (back-compat)."""
        return ModelPreset(
            description=self.description,
            thinking=self.thinking_parser,
            system_prompt_prefix=self.system_prompt_prefix,
            inference_params=dict(self.default_inference),
        )


class SamplingProfile(BaseModel):
    """Properties of a task / phase (changes freely), references a ModelProfile.

    Corresponds to the task-bound half of the old combined :class:`ModelPreset`:
    thinking mode + budget and per-task inference overrides layered on top of
    the referenced ModelProfile's ``default_inference``.
    """

    description: str = ""
    # Reference to a ModelProfile by name (in ``models:``). May be empty when
    # the model is resolved from the routing rule's ``model`` field directly.
    model: str | None = None
    thinking_mode: Literal["default", "off", "budget"] = "default"
    thinking_budget: int | None = None
    inference_overrides: dict[str, Any] = {}

    @field_validator("thinking_mode", mode="before")
    @classmethod
    def _coerce_thinking_mode(cls, v: Any) -> Any:
        """Accept YAML-bool forms (``off``→False, ``on``→True) as string literals.

        Without this, ``thinking_mode: off`` in YAML (unquoted) parses to the
        boolean ``False`` and Pydantic rejects it. See
        :func:`_coerce_yaml_bool_thinking_mode`.
        """
        return _coerce_yaml_bool_thinking_mode(v)


class ModelPreset(BaseModel):
    """DEPRECATED: combined model + sampling config.

    Retained as a back-compat alias. The legacy ``presets:`` YAML block is
    parsed into virtual :class:`ModelProfile` + :class:`SamplingProfile` pairs
    that share the preset's name; this class is used only to validate the
    legacy schema and to bridge to code paths that still expect a single object
    (e.g. ``OpenAIProvider._parse_reasoning`` legacy callers).
    """

    description: str = ""
    system_prompt_prefix: str | None = None
    thinking: ThinkingConfig | None = None
    thinking_budget_tokens: int | None = None
    inference_params: dict[str, Any] = {}


def profile_from_legacy_preset(preset: ModelPreset) -> tuple[ModelProfile, SamplingProfile]:
    """Split a legacy combined preset into a (ModelProfile, SamplingProfile) pair.

    Both profiles share the preset's name in the registry, so a legacy
    ``presets: foo:`` block is addressable as either ``models: foo:`` or
    ``sampling: foo:`` — whichever the caller asks for.
    """
    model_profile = ModelProfile(
        description=preset.description,
        thinking_parser=preset.thinking,
        system_prompt_prefix=preset.system_prompt_prefix,
        default_inference=dict(preset.inference_params),
    )
    # Legacy thinking_budget_tokens maps to a budget SamplingProfile; absent
    # budget → thinking_mode default (model's natural thinking).
    if preset.thinking_budget_tokens is not None:
        sampling = SamplingProfile(
            description=preset.description,
            thinking_mode="budget",
            thinking_budget=preset.thinking_budget_tokens,
        )
    else:
        sampling = SamplingProfile(
            description=preset.description,
            thinking_mode="default",
        )
    return model_profile, sampling


class PresetRegistry:
    """Loads ModelProfiles + SamplingProfiles from a YAML file.

    Supports both the new split format (``models:`` + ``sampling:``) and the
    legacy combined format (``presets:``). Legacy presets are split into
    virtual (ModelProfile, SamplingProfile) pairs sharing the preset name, so
    existing overlays and routing rules referencing ``preset: foo`` keep
    working unchanged.
    """

    def __init__(
        self,
        models: dict[str, ModelProfile] | None = None,
        sampling: dict[str, SamplingProfile] | None = None,
    ) -> None:
        self._models: dict[str, ModelProfile] = models or {}
        self._sampling: dict[str, SamplingProfile] = sampling or {}

    # ── Construction ────────────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: Path) -> PresetRegistry:
        """Load profiles from a YAML file.

        Returns an empty registry if the file is missing or invalid. Accepts
        both the new ``models:``/``sampling:`` format and the legacy
        ``presets:`` format (split into virtual profile pairs).
        """
        if not path.exists():
            logger.debug("Presets file not found: %s — using empty registry", path)
            return cls()

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.error("Failed to parse presets file %s: %s", path, e)
            return cls()

        models_data: dict[str, Any] = raw.get("models", {})
        sampling_data: dict[str, Any] = raw.get("sampling", {})
        legacy_presets: dict[str, Any] = raw.get("presets", {})

        models: dict[str, ModelProfile] = {}
        sampling: dict[str, SamplingProfile] = {}

        # New format: explicit models + sampling.
        for name, spec in models_data.items():
            try:
                models[name] = ModelProfile.model_validate(spec)
            except Exception as e:
                logger.warning("Invalid model profile '%s', skipping: %s", name, e)
        for name, spec in sampling_data.items():
            try:
                sampling[name] = SamplingProfile.model_validate(spec)
            except Exception as e:
                logger.warning("Invalid sampling profile '%s', skipping: %s", name, e)

        # Legacy format: combined presets → virtual (ModelProfile, SamplingProfile).
        for name, spec in legacy_presets.items():
            try:
                preset = ModelPreset.model_validate(spec)
            except Exception as e:
                logger.warning("Invalid legacy preset '%s', skipping: %s", name, e)
                continue
            m, s = profile_from_legacy_preset(preset)
            # Shared name — legacy callers addressing preset="foo" find both.
            models.setdefault(name, m)
            sampling.setdefault(name, s)

        logger.info(
            "PresetRegistry loaded %d model profiles, %d sampling profiles from %s",
            len(models),
            len(sampling),
            path,
        )
        return cls(models, sampling)

    # ── Access (new API) ────────────────────────────────────────────────────

    def get_model_profile(self, name: str) -> ModelProfile | None:
        """Return a ModelProfile by name, or None if not found."""
        return self._models.get(name)

    def get_sampling_profile(self, name: str) -> SamplingProfile | None:
        """Return a SamplingProfile by name, or None if not found."""
        return self._sampling.get(name)

    def list_model_profiles(self) -> list[str]:
        """Return all model profile names."""
        return list(self._models.keys())

    def list_sampling_profiles(self) -> list[str]:
        """Return all sampling profile names."""
        return list(self._sampling.keys())

    # ── Access (legacy back-compat) ──────────────────────────────────────────

    def get(self, name: str) -> ModelPreset | None:
        """DEPRECATED: return a legacy combined ModelPreset by name.

        Reconstructs the combined preset from the stored ModelProfile (if any).
        Only present for back-compat with callers that still expect a single
        object; new code should use ``get_model_profile`` / ``get_sampling_profile``.
        Returns None if the name is unknown.
        """
        # Prefer an explicit model profile; fall back to a sampling profile's
        # referenced model profile so legacy lookups resolve either way.
        mp = self._models.get(name)
        if mp is None:
            sp = self._sampling.get(name)
            if sp is not None and sp.model is not None:
                mp = self._models.get(sp.model)
        return mp.to_preset() if mp is not None else None

    def list_all(self) -> list[str]:
        """DEPRECATED: return all profile names (models ∪ sampling).

        Kept for back-compat with callers that iterate the registry as a flat
        preset list. New code should use ``list_model_profiles`` /
        ``list_sampling_profiles``.
        """
        seen: list[str] = []
        for name in (*self._models.keys(), *self._sampling.keys()):
            if name not in seen:
                seen.append(name)
        return seen
