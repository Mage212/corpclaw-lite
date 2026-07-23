"""Per-run workspace root (DC-017 / D-071 / B-098).

Binds the active user's workspace directory for the duration of
``AgentLoop.run`` so file tools and ``exec_script`` resolve paths relative to
``workspaces/user_<key>/`` even when containers are disabled.

Held in ``contextvars`` (same pattern as ``context_target`` / ``depth_mode``)
so concurrent runs on a shared ``AgentLoop`` singleton do not leak roots.

Honest limit: this protects path-validated tools only. Absolute-path shell
commands via ``exec_script`` still require container isolation (DC-016).
"""

from __future__ import annotations

import contextvars
from pathlib import Path

__all__ = [
    "get_workspace_root",
    "reset_workspace_root",
    "set_workspace_root",
]

_workspace_root: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "corpclaw_workspace_root", default=None
)


def set_workspace_root(root: Path | None) -> contextvars.Token[Path | None]:
    """Bind the workspace root for this run. Pass the token to ``reset_workspace_root``."""
    return _workspace_root.set(root.resolve() if root is not None else None)


def reset_workspace_root(token: contextvars.Token[Path | None]) -> None:
    """Restore the workspace root contextvar to its pre-run value."""
    _workspace_root.reset(token)


def get_workspace_root() -> Path | None:
    """Return the workspace root bound for the current run, or None."""
    return _workspace_root.get()
