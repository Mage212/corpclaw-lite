from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from corpclaw_lite.extensions.skills.base import Skill
from corpclaw_lite.extensions.tools.base import Tool

__all__ = [
    "Plugin",
    "PluginManifest",
]

logger = logging.getLogger(__name__)


ExtensionType = Literal["plugin", "skill", "subagent", "channel"]


@dataclass(frozen=True)
class PluginManifest:
    """Represents a corpclaw-lite plugin manifesto loaded from `manifest.yaml`."""

    name: str
    version: str
    type: ExtensionType
    description: str
    allowed_departments: list[str] = field(default_factory=lambda: ["*"])
    components: dict[str, str] = field(default_factory=lambda: {})
    requires: dict[str, list[str]] = field(default_factory=lambda: {})
    path: Path | None = None


@dataclass(frozen=True)
class Plugin:
    """A fully loaded plugin containing its components."""

    manifest: PluginManifest
    skill: Skill | None = None
    tools: list[Tool] = field(default_factory=lambda: [])
    scripts: list[Path] = field(default_factory=lambda: [])
