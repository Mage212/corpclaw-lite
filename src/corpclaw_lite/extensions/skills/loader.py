import logging
from pathlib import Path
from typing import Any

import yaml

from corpclaw_lite.extensions.skills.base import Skill

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
            logger.warning(f"Skill file not found: {path}")
            return None

        content = path.read_text(encoding="utf-8")
        
        if not content.startswith("---"):
            logger.warning(f"Skill file {path.name} missing YAML frontmatter.")
            return None

        # Split at the second '---'
        parts = content.split("---", 2)
        if len(parts) < 3:
            logger.warning(f"Skill file {path.name} has malformed frontmatter.")
            return None

        frontmatter_str = parts[1].strip()
        instructions = parts[2].strip()

        try:
            metadata: dict[str, Any] = yaml.safe_load(frontmatter_str) or {}
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse frontmatter in {path.name}: {e}")
            return None

        skill_id = metadata.get("id")
        if not skill_id:
            # Fallback to filename without extension
            skill_id = path.stem

        return Skill(
            id=skill_id,
            description=metadata.get("description", "No description provided."),
            allowed_for=metadata.get("allowed_for", ["*"]),
            instructions=instructions,
            path=path,
            version=metadata.get("version", "1.0.0"),
        )
