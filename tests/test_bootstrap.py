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


def test_overlay_overrides_by_filename(tmp_path: Path, caplog) -> None:
    """An overlay file with the same name overrides the default and WARNs."""
    import logging

    default_dir = tmp_path / "default"
    overlay_dir = tmp_path / "overlay"
    default_dir.mkdir()
    overlay_dir.mkdir()

    (default_dir / "SOUL.md").write_text("default soul", encoding="utf-8")
    (overlay_dir / "SOUL.md").write_text("overlay soul", encoding="utf-8")
    (overlay_dir / "CORP.md").write_text("corp-only content", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="corpclaw_lite.config.bootstrap"):
        loader = BootstrapLoader([default_dir, overlay_dir])
        prompt = loader.get_system_prompt()

    assert "overlay soul" in prompt
    assert "default soul" not in prompt  # overridden
    assert "corp-only content" in prompt  # unique overlay file added
    assert any("overridden by overlay" in r.message for r in caplog.records)


def test_overlay_department_prompt(tmp_path: Path) -> None:
    """Department prompt resolves from the highest-priority directory that has it."""
    default_dir = tmp_path / "default"
    overlay_dir = tmp_path / "overlay"
    default_dir.mkdir()
    overlay_dir.mkdir()
    (default_dir / "departments").mkdir()
    (overlay_dir / "departments").mkdir()
    (default_dir / "departments" / "hr.md").write_text("default hr", encoding="utf-8")
    (overlay_dir / "departments" / "hr.md").write_text("overlay hr", encoding="utf-8")

    loader = BootstrapLoader([default_dir, overlay_dir])
    assert loader.get_department_prompt("hr") == "overlay hr"


def test_overlay_user_prompt(tmp_path: Path) -> None:
    """Per-user prompt resolves from the highest-priority directory that has it."""
    default_dir = tmp_path / "default"
    overlay_dir = tmp_path / "overlay"
    default_dir.mkdir()
    overlay_dir.mkdir()
    (default_dir / "users").mkdir()
    (overlay_dir / "users").mkdir()
    (default_dir / "users" / "42.md").write_text("default user", encoding="utf-8")
    (overlay_dir / "users" / "42.md").write_text("overlay user", encoding="utf-8")

    loader = BootstrapLoader([default_dir, overlay_dir])
    assert loader.get_user_prompt(42) == "overlay user"


def test_overlay_hot_reload_mtime(tmp_path: Path) -> None:
    """Editing an overlay file is reflected on the next get_system_prompt() (mtime cache)."""
    import os
    import time

    default_dir = tmp_path / "default"
    overlay_dir = tmp_path / "overlay"
    default_dir.mkdir()
    overlay_dir.mkdir()
    (default_dir / "SOUL.md").write_text("default soul", encoding="utf-8")
    overlay_file = overlay_dir / "SOUL.md"
    overlay_file.write_text("overlay v1", encoding="utf-8")

    loader = BootstrapLoader([default_dir, overlay_dir])
    assert "overlay v1" in loader.get_system_prompt()

    # Bump mtime into the future so the cache sees a change.
    future = time.time() + 2
    overlay_file.write_text("overlay v2", encoding="utf-8")
    os.utime(overlay_file, (future, future))

    assert "overlay v2" in loader.get_system_prompt()
    assert "overlay v1" not in loader.get_system_prompt()


def test_single_dir_backward_compat(tmp_path: Path) -> None:
    """Passing a single Path still works as before."""
    (tmp_path / "SOUL.md").write_text("solo soul", encoding="utf-8")
    loader = BootstrapLoader(tmp_path)
    assert loader.dirs == [tmp_path]
    assert "solo soul" in loader.get_system_prompt()
