"""
Prompt building utilities for AgentLoop.

Centralises skill block construction so that runner.py and cli.py
share identical logic without duplication.
"""

from __future__ import annotations

from corpclaw_lite.extensions.skills.base import Skill

__all__ = [
    "build_skill_block",
]


def build_skill_block(
    standalone_skills: list[Skill],
    plugin_skills: list[Skill],
) -> str | None:
    """Build the '## Available Skills' markdown block for the system prompt.

    Merges standalone skills (from skills/) with plugin skills (from plugins/).
    Deduplication by skill.id: standalone skills take priority over plugin skills
    with the same id.

    Args:
        standalone_skills: Skills loaded from the standalone skills/ directory.
        plugin_skills: Skills contributed by plugins (plugin.skill values).

    Returns:
        A markdown string starting with '\\n\\n## Available Skills\\n', or
        None if there are no skills at all.
    """
    all_skills: list[Skill] = list(standalone_skills)
    seen_ids: set[str] = {s.id for s in all_skills}

    for ps in plugin_skills:
        if ps.id not in seen_ids:
            all_skills.append(ps)
            seen_ids.add(ps.id)

    if not all_skills:
        return None

    block = "\n\n## Available Skills\n"
    for s in all_skills:
        block += f"\n### {s.id}: {s.description}\n{s.instructions}\n"
    return block
