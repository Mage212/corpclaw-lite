# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""
Env-variable interpolation for YAML config files.

Supports ${VAR} and ${VAR:-default} syntax throughout nested dicts/lists.
Used by both the settings loader and MCPManager.
"""

from __future__ import annotations

import os
import re
from typing import Any

__all__ = [
    "interpolate",
    "interpolate_recursive",
]

# Matches ${VAR} and ${VAR:-default}
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def interpolate(value: str) -> str:
    """Replace ${VAR:-default} patterns with environment variable values."""

    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return os.environ.get(var.strip(), default)
        return os.environ.get(expr.strip(), "")

    return _ENV_PATTERN.sub(_replace, value)


def interpolate_recursive(obj: Any) -> Any:
    """Recursively replace ${VAR:-default} in a parsed YAML structure (dict/list/str)."""
    if isinstance(obj, str):
        return interpolate(obj)
    if isinstance(obj, dict):
        return {str(k): interpolate_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [interpolate_recursive(item) for item in obj]
    return obj
