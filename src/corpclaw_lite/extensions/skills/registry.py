from __future__ import annotations

import logging
from pathlib import Path

from corpclaw_lite.extensions.skills.base import Skill
from corpclaw_lite.extensions.skills.loader import SkillLoader
from corpclaw_lite.users.models import User

__all__ = [
    "SkillRegistry",
]

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Manages loaded skills and provides access control."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def load_directory(self, skills_dir: Path | str, *, allow_replace: bool = False) -> None:
        """Load all .md files from a directory.

        When ``allow_replace=True`` (overlay loading), a skill with the same id
        as an existing one overrides it and a WARN is logged.
        """
        dir_path = Path(skills_dir)
        if not dir_path.exists() or not dir_path.is_dir():
            logger.warning("Skills directory not found: %s", dir_path)
            return

        loaded_count = 0
        for md_file in dir_path.glob("*.md"):
            skill = SkillLoader.load_from_file(md_file)
            if skill:
                self.register(skill, allow_replace=allow_replace)
                loaded_count += 1

        logger.info("Loaded %d skills from %s", loaded_count, dir_path)

    def register(self, skill: Skill, *, allow_replace: bool = False) -> None:
        """Register a single skill."""
        if skill.id in self._skills and not allow_replace:
            raise ValueError(f"Skill '{skill.id}' is already registered.")
        if skill.id in self._skills and allow_replace:
            logger.warning("Skill '%s' overridden by overlay: %s", skill.id, skill.path)
        self._skills[skill.id] = skill

    def unregister(self, skill_id: str) -> None:
        """Remove a skill by ID (no-op if not found)."""
        self._skills.pop(skill_id, None)

    def get(self, skill_id: str) -> Skill | None:
        """Get a skill by ID. Alias for get_skill."""
        return self._skills.get(skill_id)

    def get_skill(self, skill_id: str) -> Skill | None:
        """Get a skill by ID without checking permissions."""
        return self._skills.get(skill_id)

    def list_all(self) -> list[Skill]:
        """List all loaded skills."""
        return list(self._skills.values())

    def items(self) -> dict[str, Skill]:
        """Return a copy of the id→skill mapping."""
        return dict(self._skills)

    def get_allowed_skills(self, user: User) -> list[Skill]:
        """Return only the skills the user is allowed to see (based on their department)."""
        return [
            skill
            for skill in self._skills.values()
            if "*" in skill.allowed_for or user.department in skill.allowed_for
        ]
