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

import os
from pathlib import Path
from typing import Any

import yaml

from corpclaw_lite.config.interpolation import interpolate_recursive
from corpclaw_lite.config.settings import Settings

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
        # Resolve project root (4 levels up from this file)
        project_root = (
            Path(os.environ.get("CORPCLAW_ROOT", "")) or Path(__file__).parent.parent.parent.parent
        )
        path = project_root / "config" / "settings.yaml"

    yaml_path = Path(path)
    if not yaml_path.exists():
        return Settings()

    raw = yaml_path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    interpolated: dict[str, Any] = interpolate_recursive(data)

    # Filter out empty strings for optional secrets (api_key, base_url)
    # so Pydantic uses None defaults instead of ""
    _clean_empty_strings(interpolated)

    return Settings.model_validate(interpolated)


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
