from pathlib import Path

from corpclaw_lite.extensions.plugins.loader import PluginLoader
from corpclaw_lite.extensions.plugins.registry import PluginRegistry
from corpclaw_lite.users.models import User


def test_plugin_loader(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "test_plugin"
    plugin_dir.mkdir()
    
    manifest_file = plugin_dir / "manifest.yaml"
    manifest_file.write_text(
        "name: test_plugin\n"
        "version: '2.0'\n"
        "type: plugin\n"
        "allowed_departments: [dev]\n"
        "components:\n"
        "  skill: skill.md\n"
        "  script: run.py\n"
    )
    
    skill_file = plugin_dir / "skill.md"
    skill_file.write_text("---\nid: p_skill\n---\ninstructions")
    
    script_file = plugin_dir / "run.py"
    script_file.write_text("print('ok')")
    
    plugin = PluginLoader.load_plugin(plugin_dir)
    assert plugin is not None
    assert plugin.manifest.name == "test_plugin"
    assert plugin.manifest.version == "2.0"
    assert plugin.manifest.allowed_departments == ["dev"]
    assert plugin.skill is not None
    assert plugin.skill.id == "p_skill"
    assert len(plugin.scripts) == 1
    assert plugin.scripts[0] == script_file

def test_plugin_registry(tmp_path: Path) -> None:
    registry = PluginRegistry()
    
    # Create plugin 1 (allowed for all)
    p1_dir = tmp_path / "p1"
    p1_dir.mkdir()
    (p1_dir / "manifest.yaml").write_text("name: p1\nallowed_departments: ['*']\n")
    
    # Create plugin 2 (allowed for hr)
    p2_dir = tmp_path / "p2"
    p2_dir.mkdir()
    (p2_dir / "manifest.yaml").write_text("name: p2\nallowed_departments: [hr]\n")
    
    registry.load_directory(tmp_path)
    assert len(registry.list_all()) == 2
    
    user_hr = User(id=1, name="H", department="hr")
    user_sales = User(id=2, name="S", department="sales")
    
    assert len(registry.get_allowed_plugins(user_hr)) == 2
    assert len(registry.get_allowed_plugins(user_sales)) == 1
    assert registry.get_allowed_plugins(user_sales)[0].manifest.name == "p1"
