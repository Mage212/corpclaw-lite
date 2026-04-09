"""Tests for PluginHotReloader — directory watch + targeted reload."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.extensions.plugins.registry import PluginRegistry
from corpclaw_lite.extensions.plugins.watcher import PluginHotReloader
from corpclaw_lite.extensions.skills.registry import SkillRegistry
from corpclaw_lite.extensions.tools.registry import ToolRegistry


def _write_plugin(plugin_dir: Path, name: str) -> None:
    """Write a minimal plugin with skill.md only."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "manifest.yaml").write_text(
        f"name: {name}\nversion: '1.0'\ntype: plugin\ndescription: Test\n"
        f"allowed_departments: ['*']\ncomponents:\n  skill: skill.md\n",
        encoding="utf-8",
    )
    (plugin_dir / "skill.md").write_text(
        f"---\nid: skill_{name}\ndescription: Skill for {name}\nallowed_for: ['*']\n---\n"
        f"Instructions for {name}.",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_scan_initial_load(tmp_path: Path) -> None:
    """First scan registers plugins found in the directory."""
    plugin_dir = tmp_path / "plugin_a"
    _write_plugin(plugin_dir, "plugin_a")

    tool_registry = ToolRegistry()
    skill_registry = SkillRegistry()
    plugin_registry = PluginRegistry()

    reloader = PluginHotReloader(tmp_path, plugin_registry, tool_registry, skill_registry)
    await reloader._scan()

    assert plugin_registry.get_plugin("plugin_a") is not None


@pytest.mark.asyncio
async def test_scan_detects_new_plugin(tmp_path: Path) -> None:
    """A plugin added after the first scan is detected and registered."""
    tool_registry = ToolRegistry()
    skill_registry = SkillRegistry()
    plugin_registry = PluginRegistry()

    reloader = PluginHotReloader(tmp_path, plugin_registry, tool_registry, skill_registry)
    await reloader._scan()  # empty first pass

    _write_plugin(tmp_path / "new_plugin", "new_plugin")
    await reloader._scan()

    assert plugin_registry.get_plugin("new_plugin") is not None


@pytest.mark.asyncio
async def test_scan_detects_removed_plugin(tmp_path: Path) -> None:
    """A plugin directory removed between scans is unregistered."""
    plugin_dir = tmp_path / "removable"
    _write_plugin(plugin_dir, "removable")

    tool_registry = ToolRegistry()
    skill_registry = SkillRegistry()
    plugin_registry = PluginRegistry()

    reloader = PluginHotReloader(tmp_path, plugin_registry, tool_registry, skill_registry)
    await reloader._scan()  # registers 'removable'
    assert plugin_registry.get_plugin("removable") is not None

    # Remove the plugin directory
    import shutil

    shutil.rmtree(plugin_dir)
    await reloader._scan()

    assert plugin_registry.get_plugin("removable") is None


@pytest.mark.asyncio
async def test_scan_detects_changed_plugin(tmp_path: Path) -> None:
    """A plugin with updated mtime is reloaded with new content."""
    plugin_dir = tmp_path / "changing"
    _write_plugin(plugin_dir, "changing")

    tool_registry = ToolRegistry()
    skill_registry = SkillRegistry()
    plugin_registry = PluginRegistry()

    reloader = PluginHotReloader(tmp_path, plugin_registry, tool_registry, skill_registry)
    await reloader._scan()

    # Simulate mtime going back so next modification is detected
    reloader._mtimes[plugin_dir] = 0.0

    # Update the skill description
    (plugin_dir / "skill.md").write_text(
        "---\nid: skill_changing\ndescription: Updated description\nallowed_for: ['*']\n---\n"
        "New instructions.",
        encoding="utf-8",
    )
    await reloader._scan()

    plugin = plugin_registry.get_plugin("changing")
    assert plugin is not None
    assert plugin.skill is not None
    assert plugin.skill.description == "Updated description"


@pytest.mark.asyncio
async def test_unregister_plugin_removes_skill(tmp_path: Path) -> None:
    """_unregister_plugin removes the skill from SkillRegistry."""
    plugin_dir = tmp_path / "with_skill"
    _write_plugin(plugin_dir, "with_skill")

    tool_registry = ToolRegistry()
    skill_registry = SkillRegistry()
    plugin_registry = PluginRegistry()

    reloader = PluginHotReloader(tmp_path, plugin_registry, tool_registry, skill_registry)
    await reloader._scan()

    # Skill should be in registry after scan
    assert skill_registry.get_skill("skill_with_skill") is not None

    import shutil

    shutil.rmtree(plugin_dir)
    await reloader._scan()

    assert skill_registry.get_skill("skill_with_skill") is None


def test_force_reload_with_subprocess_isolation(tmp_path: Path) -> None:
    """load_plugin loads tool via subprocess introspection (no sys.modules pollution)."""
    from corpclaw_lite.extensions.plugins.loader import PluginLoader
    from corpclaw_lite.extensions.plugins.sandbox_proxy import PluginToolProxy

    plugin_dir = tmp_path / "cached_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.yaml").write_text(
        "name: cached_plugin\nversion: '1.0'\ntype: plugin\ndescription: Cached\n"
        "components:\n  tool: tool.py\n",
        encoding="utf-8",
    )
    (plugin_dir / "tool.py").write_text(
        "from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam\n"
        "class DummyCachedTool(Tool):\n"
        "    name='cached_tool'\n"
        "    description='cached'\n"
        "    params: list[ToolParam] = []\n"
        "    risk_level = RiskLevel.LOW\n"
        "    async def execute(self, **kwargs): return 'old'\n",
        encoding="utf-8",
    )

    # Load — tool.py runs in subprocess, NOT in main process
    plugin = PluginLoader.load_plugin(plugin_dir)
    assert plugin is not None
    assert len(plugin.tools) == 1
    assert isinstance(plugin.tools[0], PluginToolProxy)
    assert plugin.tools[0].name == "cached_tool"

    # reload should also work (new proxy, no stale state)
    plugin2 = PluginLoader.load_plugin(plugin_dir)
    assert plugin2 is not None
    assert isinstance(plugin2.tools[0], PluginToolProxy)
