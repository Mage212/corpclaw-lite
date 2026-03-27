"""Tests for BootstrapLoader — system prompt assembly and caching."""

from __future__ import annotations

from pathlib import Path

from corpclaw_lite.config.bootstrap import BootstrapLoader


def test_get_system_prompt_missing_dir(tmp_path: Path) -> None:
    """Returns empty string when bootstrap dir doesn't exist."""
    loader = BootstrapLoader(tmp_path / "nonexistent")
    assert loader.get_system_prompt() == ""


def test_get_system_prompt_from_files(tmp_path: Path) -> None:
    """Assembles prompt from sorted .md files."""
    (tmp_path / "01_SOUL.md").write_text("You are helpful.", encoding="utf-8")
    (tmp_path / "02_COMPANY.md").write_text("Company context.", encoding="utf-8")

    loader = BootstrapLoader(tmp_path)
    prompt = loader.get_system_prompt()
    assert "You are helpful." in prompt
    assert "Company context." in prompt
    assert prompt.index("You are helpful.") < prompt.index("Company context.")


def test_get_system_prompt_with_extras(tmp_path: Path) -> None:
    """Extras dict is appended to the prompt."""
    (tmp_path / "base.md").write_text("Base.", encoding="utf-8")
    loader = BootstrapLoader(tmp_path)
    prompt = loader.get_system_prompt(extras={"Skills": "skill list"})
    assert "Base." in prompt
    assert "## Skills" in prompt
    assert "skill list" in prompt


def test_caching(tmp_path: Path) -> None:
    """Subsequent calls use cache if mtime unchanged."""
    f = tmp_path / "test.md"
    f.write_text("v1", encoding="utf-8")

    loader = BootstrapLoader(tmp_path)
    p1 = loader.get_system_prompt()
    p2 = loader.get_system_prompt()  # should use cache
    assert p1 == p2 == "v1"


def test_render_skills_section_empty() -> None:
    """Empty skills list returns empty string."""
    loader = BootstrapLoader("nonexistent")
    assert loader.render_skills_section([]) == ""


def test_render_skills_section() -> None:
    """Skills are formatted as markdown list."""
    loader = BootstrapLoader("nonexistent")
    result = loader.render_skills_section([("s1", "desc1"), ("s2", "desc2")])
    assert "**s1**: desc1" in result
    assert "**s2**: desc2" in result


def test_get_department_prompt_missing(tmp_path: Path) -> None:
    """Returns None when department file doesn't exist."""
    loader = BootstrapLoader(tmp_path)
    assert loader.get_department_prompt("marketing") is None


def test_get_department_prompt_exists(tmp_path: Path) -> None:
    """Returns content from department md file."""
    dept_dir = tmp_path / "departments"
    dept_dir.mkdir()
    (dept_dir / "marketing.md").write_text("Marketing rules.", encoding="utf-8")

    loader = BootstrapLoader(tmp_path)
    result = loader.get_department_prompt("marketing")
    assert result == "Marketing rules."
