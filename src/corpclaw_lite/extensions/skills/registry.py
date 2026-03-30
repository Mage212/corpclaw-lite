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

    def load_directory(self, skills_dir: Path | str) -> None:
        """Load all .md files from a directory."""
        dir_path = Path(skills_dir)
        if not dir_path.exists() or not dir_path.is_dir():
            logger.warning("Skills directory not found: %s", dir_path)
            return

        loaded_count = 0
        for md_file in dir_path.glob("*.md"):
            skill = SkillLoader.load_from_file(md_file)
            if skill:
                self.register(skill)
                loaded_count += 1

        logger.info("Loaded %d skills from %s", loaded_count, dir_path)

    def register(self, skill: Skill) -> None:
        """Register a single skill."""
        self._skills[skill.id] = skill

    def unregister(self, skill_id: str) -> None:
        """Remove a skill by ID (no-op if not found)."""
        self._skills.pop(skill_id, None)

    def get_skill(self, skill_id: str) -> Skill | None:
        """Get a skill by ID without checking permissions."""
        return self._skills.get(skill_id)

    def list_all(self) -> list[Skill]:
        """List all loaded skills."""
        return list(self._skills.values())

    def get_allowed_skills(self, user: User) -> list[Skill]:
        """Return only the skills the user is allowed to see (based on their department)."""
        allowed: list[Skill] = []
        for skill in self._skills.values():
            if "*" in skill.allowed_for or user.department in skill.allowed_for:
                allowed.append(skill)
        return allowed
