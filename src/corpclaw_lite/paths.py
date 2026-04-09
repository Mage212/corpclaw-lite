from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "DATA_DIR",
    "PROJECT_ROOT",
]


def get_project_root() -> Path:
    env = os.environ.get("CORPCLAW_ROOT", "")
    if env:
        return Path(env)
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    raise RuntimeError("Cannot find project root (no pyproject.toml found)")


PROJECT_ROOT = get_project_root()
DATA_DIR = Path(os.environ.get("CORPCLAW_DATA_DIR", "") or PROJECT_ROOT / "data")
