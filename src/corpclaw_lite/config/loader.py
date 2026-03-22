from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from corpclaw_lite.config.settings import Settings

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(node: Any) -> Any:
    """Expand ${VAR:-default} and ${VAR} in strings within YAML config."""
    if isinstance(node, dict):
        return {k: _expand_env_vars(v) for k, v in node.items()}
    elif isinstance(node, list):
        return [_expand_env_vars(item) for item in node]
    elif isinstance(node, str):
        if not node:
            return node

        def replace_env(match: re.Match[str]) -> str:
            expr = match.group(1)
            if ":-" in expr:
                var_name, default = expr.split(":-", 1)
            else:
                var_name, default = expr, ""
            return os.environ.get(var_name, default)

        # Apply replacements
        return _ENV_PATTERN.sub(replace_env, node)
    return node


def load_settings(path: Path | str) -> Settings:
    """Load settings from a YAML file, expanding env vars."""
    config_path = Path(path)
    if not config_path.exists():
        return Settings()

    try:
        with open(config_path, encoding="utf-8") as f:
            raw_yaml = yaml.safe_load(f) or {}
    except Exception as e:
        raise ValueError(f"Failed to load config {path}: {e}") from e

    # Expand variables
    expanded = _expand_env_vars(raw_yaml)

    # Initialize settings from expanded dict
    return Settings.model_validate(expanded)
