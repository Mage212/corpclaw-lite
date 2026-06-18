"""Central resolver for extension paths (mirror-layout overlay).

Single source of truth for where each extension kind is loaded from:
``resolve_dirs(kind, settings, project_root)`` returns the ordered list of
paths (default first, overlays later). Later in the list = higher priority
(overlay-override, applied in PR-2).

Mirror-layout: every ``extra_path`` mirrors the project structure —
``<extra>/skills/``, ``<extra>/plugins/``, ``<extra>/config/subagents/``,
``<extra>/config/bootstrap/``, ``<extra>/config/mcp_servers.yaml``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from corpclaw_lite.config.settings import Settings

__all__ = [
    "ExtensionKind",
    "resolve_dirs",
]

logger = logging.getLogger(__name__)

ExtensionKind = Literal["skills", "plugins", "subagents", "mcp", "bootstrap"]

# kind → subpath relative to project_root (and mirrored under each extra_path).
_KIND_SUBPATH: dict[ExtensionKind, str] = {
    "skills": "skills",
    "plugins": "plugins",
    "subagents": "config/subagents",
    "bootstrap": "config/bootstrap",
    "mcp": "config/mcp_servers.yaml",
}


def resolve_dirs(
    kind: ExtensionKind,
    settings: Settings,
    project_root: Path,
) -> list[Path]:
    """Ordered paths for ``kind``: ``[default, ...overlays]``.

    The default path (``project_root / <subpath>``) is always returned first
    regardless of existence — call-sites decide how to handle a missing
    default (they already do today).

    Each ``extra_path`` contributes ``<extra>/<subpath>``. Entries are skipped
    when the raw string is empty/whitespace (guards against an unresolved
    ``${VAR}`` collapsing to ``""``, where ``Path("") / "skills" == "skills"``
    and would silently resolve against the cwd) or when the resolved path does
    not exist on disk. Skips are logged at debug level.
    """
    subpath = _KIND_SUBPATH[kind]
    resolved: list[Path] = [(project_root / subpath).resolve()]

    for raw in settings.extensions.extra_paths:
        stripped = raw.strip()
        if not stripped:
            logger.debug("extensions: skip empty extra_path for %s", kind)
            continue
        candidate = (Path(stripped) / subpath).resolve()
        if not candidate.exists():
            logger.debug("extensions: skip missing path for %s: %s", kind, candidate)
            continue
        resolved.append(candidate)

    return resolved
