# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""
Settings loader that reads config/settings.yaml with ${VAR:-default} env-interpolation.

Separation of concerns:
  - config/settings.yaml  – all provider definitions, routing rules, agent parameters
  - .env                  – secrets only (API keys, tokens)

Usage:
    from corpclaw_lite.config.loader import load_settings
    settings = load_settings()  # reads PROJECT_ROOT/config/settings.yaml
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from corpclaw_lite.config.interpolation import interpolate_recursive
from corpclaw_lite.config.settings import Settings
from corpclaw_lite.paths import PROJECT_ROOT

__all__ = ["load_settings"]


def load_settings(path: Path | str | None = None) -> Settings:
    """Load Settings from a YAML file with env-variable interpolation.

    Args:
        path: Path to settings.yaml. Defaults to PROJECT_ROOT/config/settings.yaml.
              Falls back to default Settings() if file not found.

    Returns:
        Fully populated Settings instance.
    """
    if path is None:
        path = PROJECT_ROOT / "config" / "settings.yaml"

    yaml_path = Path(path)
    if not yaml_path.exists():
        return Settings()

    raw = yaml_path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    interpolated: dict[str, Any] = interpolate_recursive(data)

    # Filter out empty strings for optional secrets (api_key, base_url)
    # so Pydantic uses None defaults instead of ""
    _clean_empty_strings(interpolated)

    settings = Settings.model_validate(interpolated)

    # Merge calibrated settings override if present
    calibrated_override = yaml_path.parent / "calibrated" / "settings_override.yaml"
    if calibrated_override.exists():
        from corpclaw_lite.config.settings import AgentSettings

        override_raw = yaml.safe_load(calibrated_override.read_text(encoding="utf-8")) or {}
        if override_raw and "agent" in override_raw:
            merged: dict[str, Any] = {**settings.agent.model_dump(), **override_raw["agent"]}
            settings.agent = AgentSettings.model_validate(merged)

    return settings


def _clean_empty_strings(obj: Any) -> None:
    """Replace empty string values with None for optional fields in-place."""
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            value = obj[key]
            if value == "":
                obj[key] = None
            else:
                _clean_empty_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            _clean_empty_strings(item)
