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
import re
from pathlib import Path
from typing import Any

import yaml

from corpclaw_lite.config.settings import Settings

__all__ = ["load_settings"]

# Matches ${VAR} and ${VAR:-default}
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate(value: str) -> str:
    """Replace ${VAR:-default} patterns with environment values."""

    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return os.environ.get(var.strip(), default)
        return os.environ.get(expr.strip(), "")

    return _ENV_PATTERN.sub(_replace, value)


def _interpolate_recursive(obj: Any) -> Any:
    """Recursively interpolate env variables in a parsed YAML structure."""
    if isinstance(obj, str):
        return _interpolate(obj)
    if isinstance(obj, dict):
        return {str(k): _interpolate_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_recursive(item) for item in obj]
    return obj


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
    interpolated: dict[str, Any] = _interpolate_recursive(data)

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
