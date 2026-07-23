from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.extensions.skills.registry import SkillRegistry
from corpclaw_lite.extensions.skills.watcher import SkillHotReloader


def _write_skill(path: Path, *, description: str = "Initial") -> None:
    path.write_text(
        "---\n"
        "id: test-skill\n"
        f"description: {description}\n"
        "version: '1.0.0'\n"
        "allowed_for:\n"
        "  - '*'\n"
        "keywords:\n"
        "  - test\n"
        "---\n\n"
        "Test instructions.\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio()
async def test_skill_hot_reloader_loads_updates_and_unregisters(tmp_path: Path) -> None:
    registry = SkillRegistry()
    skill_path = tmp_path / "test-skill.md"
    _write_skill(skill_path)

    reloader = SkillHotReloader(tmp_path, registry)
    await reloader._scan()

    skill = registry.get("test-skill")
    assert skill is not None
    assert skill.description == "Initial"

    reloader._mtimes[skill_path] = 0.0
    _write_skill(skill_path, description="Updated")
    await reloader._scan()

    updated = registry.get("test-skill")
    assert updated is not None
    assert updated.description == "Updated"

    skill_path.unlink()
    await reloader._scan()

    assert registry.get("test-skill") is None


@pytest.mark.asyncio()
async def test_skill_hot_reloader_prime_does_not_re_register_bootstrap_loaded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: when the bootstrap loader has already registered a skill,
    the watcher's startup prime + first scan must NOT re-register it and emit a
    spurious 'overridden by overlay' WARNING.

    Before the fix, the first scan treated every on-disk file as "new" (mtime
    unprimed) and re-registered, hitting the allow_replace overlay branch.
    """
    registry = SkillRegistry()
    skill_path = tmp_path / "test-skill.md"
    _write_skill(skill_path)

    # Simulate the bootstrap loader registering the skill from the same file.
    registry.load_directory(tmp_path)

    reloader = SkillHotReloader(tmp_path, registry)
    with caplog.at_level("WARNING"):
        # _poll_loop does prime() then scan(); we exercise both directly.
        await reloader._prime()
        await reloader._scan()

    overlay_warnings = [r for r in caplog.records if "overridden by overlay" in r.getMessage()]
    assert overlay_warnings == [], "startup prime should not re-register bootstrap-loaded skills"

    # The bootstrap-loaded skill is still present and unchanged.
    skill = registry.get("test-skill")
    assert skill is not None
    assert skill.description == "Initial"


@pytest.mark.asyncio()
async def test_skill_hot_reloader_ignores_missing_dir(tmp_path: Path) -> None:
    registry = SkillRegistry()
    missing = tmp_path / "missing"

    reloader = SkillHotReloader(missing, registry)
    await reloader._scan()
    assert registry.list_all() == []
