"""Tests for SkillLoader — parsing markdown skills with YAML frontmatter."""

from __future__ import annotations

from pathlib import Path

from corpclaw_lite.extensions.skills.loader import SkillLoader


def test_load_valid_skill(tmp_path: Path) -> None:
    """Properly formatted skill file is loaded."""
    skill_file = tmp_path / "test_skill.md"
    skill_file.write_text(
        "---\nid: my_skill\ndescription: Does stuff\n"
        "allowed_for: ['*']\nversion: '2.0'\n---\nDo this and that.\n",
        encoding="utf-8",
    )
    skill = SkillLoader.load_from_file(skill_file)
    assert skill is not None
    assert skill.id == "my_skill"
    assert skill.description == "Does stuff"
    assert skill.allowed_for == ["*"]
    assert skill.version == "2.0"
    assert skill.instructions == "Do this and that."


def test_load_missing_file(tmp_path: Path) -> None:
    """Missing file returns None."""
    assert SkillLoader.load_from_file(tmp_path / "nope.md") is None


def test_load_no_frontmatter(tmp_path: Path) -> None:
    """File without --- frontmatter returns None."""
    f = tmp_path / "plain.md"
    f.write_text("Just markdown, no frontmatter.", encoding="utf-8")
    assert SkillLoader.load_from_file(f) is None


def test_load_malformed_frontmatter(tmp_path: Path) -> None:
    """File with only one --- separator returns None."""
    f = tmp_path / "bad.md"
    f.write_text("---\nid: x\nno closing separator", encoding="utf-8")
    assert SkillLoader.load_from_file(f) is None


def test_load_fallback_id_from_filename(tmp_path: Path) -> None:
    """When frontmatter has no 'id', fallback to filename stem."""
    f = tmp_path / "fallback_skill.md"
    f.write_text(
        "---\ndescription: No explicit id\n---\nInstructions here.",
        encoding="utf-8",
    )
    skill = SkillLoader.load_from_file(f)
    assert skill is not None
    assert skill.id == "fallback_skill"


def test_load_invalid_yaml(tmp_path: Path) -> None:
    """Invalid YAML in frontmatter returns None."""
    f = tmp_path / "bad_yaml.md"
    f.write_text("---\n: : : invalid\n---\nContent.", encoding="utf-8")
    assert SkillLoader.load_from_file(f) is None
