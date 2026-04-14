from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from corpclaw_lite.extensions.skills.base import Skill

__all__ = [
    "SkillLoader",
]

logger = logging.getLogger(__name__)


class SkillLoader:
    """Loads Skill objects from markdown files with YAML frontmatter."""

    @classmethod
    def load_from_file(cls, path: Path) -> Skill | None:
        """Parse a markdown file and return a Skill object.

        Expected format:
        ---
        id: my_skill
        description: A description
        allowed_for: ["*"]
        version: "1.0.0"
        ---
        Full markdown instructions...
        """
        if not path.exists() or not path.is_file():
            logger.warning("Skill file not found: %s", path)
            return None

        content = path.read_text(encoding="utf-8")

        if not content.startswith("---"):
            logger.warning("Skill file %s missing YAML frontmatter.", path.name)
            return None

        # Split at the second '---'
        parts = content.split("---", 2)
        if len(parts) < 3:
            logger.warning("Skill file %s has malformed frontmatter.", path.name)
            return None

        frontmatter_str = parts[1].strip()
        instructions = parts[2].strip()

        try:
            metadata: dict[str, Any] = yaml.safe_load(frontmatter_str) or {}
        except yaml.YAMLError as e:
            logger.error("Failed to parse frontmatter in %s: %s", path.name, e)
            return None

        skill_id = metadata.get("id")
        if not skill_id:
            # Fallback to filename without extension
            skill_id = path.stem

        # Apply calibrated instruction override if present
        try:
            from corpclaw_lite.paths import PROJECT_ROOT

            calibrated_path = PROJECT_ROOT / "config" / "calibrated" / "skills" / f"{skill_id}.md"
            if calibrated_path.exists():
                instructions = calibrated_path.read_text(encoding="utf-8").strip()
                logger.debug("Skill '%s': using calibrated instructions", skill_id)
        except Exception:
            pass  # Fall back to original instructions on any error

        return Skill(
            id=skill_id,
            description=metadata.get("description", "No description provided."),
            allowed_for=metadata.get("allowed_for", ["*"]),
            instructions=instructions,
            path=path,
            version=metadata.get("version", "1.0.0"),
            keywords=metadata.get("keywords", []),
            always=metadata.get("always", False),
        )
