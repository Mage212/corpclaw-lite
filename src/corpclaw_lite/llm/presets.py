# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Model presets — named sets of inference params and thinking configuration.

A preset bundles:
  - inference_params:  temperature, top_p, top_k, etc. (merged with request-level)
  - thinking config:   how to extract reasoning from model output
  - system_prompt_prefix:  injected before the system prompt (e.g. ``<|think|>``)
  - thinking_budget_tokens:  cap on reasoning output

Presets are defined in ``config/model_presets.yaml`` and referenced by name
in ``config/settings.yaml`` via the ``preset`` field on each named provider.

Priority: request-level params > preset params > provider defaults.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

__all__ = [
    "ModelPreset",
    "PresetRegistry",
    "ThinkingConfig",
]

logger = logging.getLogger(__name__)


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


class ModelPreset(BaseModel):
    """A named set of inference parameters and thinking configuration for a model.

    All fields are optional — each preset contains only what the model needs.
    """

    description: str = ""
    system_prompt_prefix: str | None = None
    thinking: ThinkingConfig | None = None
    thinking_budget_tokens: int | None = None
    inference_params: dict[str, Any] = {}


class PresetRegistry:
    """Loads and provides model presets from a YAML file."""

    def __init__(self, presets: dict[str, ModelPreset] | None = None) -> None:
        self._presets: dict[str, ModelPreset] = presets or {}

    @classmethod
    def from_yaml(cls, path: Path) -> PresetRegistry:
        """Load presets from a YAML file.

        Returns an empty registry if the file is missing or invalid.
        """
        if not path.exists():
            logger.debug("Presets file not found: %s — using empty registry", path)
            return cls()

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.error("Failed to parse presets file %s: %s", path, e)
            return cls()

        presets_data: dict[str, Any] = raw.get("presets", {})
        presets: dict[str, ModelPreset] = {}
        for name, spec in presets_data.items():
            try:
                presets[name] = ModelPreset.model_validate(spec)
                logger.debug("Loaded preset '%s': %s", name, presets[name].description)
            except Exception as e:
                logger.warning("Invalid preset '%s', skipping: %s", name, e)

        logger.info("PresetRegistry loaded %d presets from %s", len(presets), path)
        return cls(presets)

    def get(self, name: str) -> ModelPreset | None:
        """Return a preset by name, or None if not found."""
        return self._presets.get(name)

    def list_all(self) -> list[str]:
        """Return all preset names."""
        return list(self._presets.keys())
