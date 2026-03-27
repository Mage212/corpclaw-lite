"""Tests for PluginLoader — loading plugins from directories with manifest.yaml."""

from __future__ import annotations

from pathlib import Path

from corpclaw_lite.extensions.plugins.loader import PluginLoader


def test_load_manifest_valid(tmp_path: Path) -> None:
    """Valid manifest.yaml is parsed correctly."""
    manifest_file = tmp_path / "manifest.yaml"
    manifest_file.write_text(
        "name: test_plugin\nversion: '1.0.0'\ntype: plugin\ndescription: Test\n",
        encoding="utf-8",
    )
    manifest = PluginLoader.load_manifest(manifest_file)
    assert manifest is not None
    assert manifest.name == "test_plugin"
    assert manifest.version == "1.0.0"


def test_load_manifest_missing() -> None:
    """Missing manifest returns None."""
    assert PluginLoader.load_manifest(Path("/fake/path/manifest.yaml")) is None


def test_load_plugin_missing_dir() -> None:
    """Non-existent plugin directory returns None."""
    assert PluginLoader.load_plugin(Path("/fake/plugin_dir")) is None


def test_load_plugin_no_manifest(tmp_path: Path) -> None:
    """Plugin dir without manifest.yaml returns None."""
    plugin_dir = tmp_path / "myplugin"
    plugin_dir.mkdir()
    assert PluginLoader.load_plugin(plugin_dir) is None


def test_load_plugin_with_skill(tmp_path: Path) -> None:
    """Plugin with skill.md component loads the skill."""
    plugin_dir = tmp_path / "skill_plugin"
    plugin_dir.mkdir()

    (plugin_dir / "manifest.yaml").write_text(
        "name: skill_plugin\nversion: '1.0'\ntype: plugin\n"
        "description: Has a skill\ncomponents:\n  skill: skill.md\n",
        encoding="utf-8",
    )
    (plugin_dir / "skill.md").write_text(
        "---\nid: plugin_skill\ndescription: From plugin\n---\nDo work.\n",
        encoding="utf-8",
    )

    plugin = PluginLoader.load_plugin(plugin_dir)
    assert plugin is not None
    assert plugin.skill is not None
    assert plugin.skill.id == "plugin_skill"


def test_load_plugin_path_traversal_blocked(tmp_path: Path) -> None:
    """Skill path traversal is blocked."""
    plugin_dir = tmp_path / "evil_plugin"
    plugin_dir.mkdir()

    (plugin_dir / "manifest.yaml").write_text(
        "name: evil\nversion: '1.0'\ntype: plugin\n"
        "description: Evil\ncomponents:\n  skill: ../../../etc/passwd\n",
        encoding="utf-8",
    )

    plugin = PluginLoader.load_plugin(plugin_dir)
    assert plugin is None


def test_load_plugin_with_script(tmp_path: Path) -> None:
    """Plugin with script component includes the script path."""
    plugin_dir = tmp_path / "script_plugin"
    plugin_dir.mkdir()

    (plugin_dir / "manifest.yaml").write_text(
        "name: script_plugin\nversion: '1.0'\ntype: plugin\n"
        "description: Has script\ncomponents:\n  script: run.sh\n",
        encoding="utf-8",
    )
    (plugin_dir / "run.sh").write_text("#!/bin/bash\necho hello", encoding="utf-8")

    plugin = PluginLoader.load_plugin(plugin_dir)
    assert plugin is not None
    assert len(plugin.scripts) == 1
    assert plugin.scripts[0].name == "run.sh"
