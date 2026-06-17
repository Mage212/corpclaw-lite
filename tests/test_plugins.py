from pathlib import Path

import pytest

from corpclaw_lite.config.settings import Settings
from corpclaw_lite.departments.manager import DepartmentConfig, DepartmentManager
from corpclaw_lite.departments.permissions import PermissionChecker
from corpclaw_lite.extensions.plugins.base import Plugin, PluginManifest
from corpclaw_lite.extensions.plugins.loader import PluginLoader
from corpclaw_lite.extensions.plugins.registry import PluginRegistry
from corpclaw_lite.extensions.tools.base import Tool
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User


class DummyPluginTool(Tool):
    name = "plugin_tool"
    description = "Plugin tool"
    params = []

    async def execute(self, **kwargs: object) -> str:
        return "ok"


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


def test_plugin_path_traversal_blocked(tmp_path: Path) -> None:
    """Plugin with path traversal in component filenames must be rejected."""
    plugin_dir = tmp_path / "evil_plugin"
    plugin_dir.mkdir()

    manifest_file = plugin_dir / "manifest.yaml"
    manifest_file.write_text(
        "name: evil_plugin\nversion: '1.0'\ntype: plugin\ncomponents:\n  skill: ../../etc/passwd\n"
    )

    plugin = PluginLoader.load_plugin(plugin_dir)
    # Should return None because skill path escapes the plugin directory
    assert plugin is None


def test_plugin_tool_path_traversal_blocked(tmp_path: Path) -> None:
    """Plugin with path traversal in tool filename must be rejected."""
    plugin_dir = tmp_path / "evil_tool_plugin"
    plugin_dir.mkdir()

    manifest_file = plugin_dir / "manifest.yaml"
    manifest_file.write_text(
        "name: evil_tool\nversion: '1.0'\ntype: plugin\ncomponents:\n  tool: ../../malicious.py\n"
    )

    plugin = PluginLoader.load_plugin(plugin_dir)
    assert plugin is None


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


def test_plugin_registry_overlay_replace(tmp_path: Path) -> None:
    """Overlay dir overrides a default plugin by name when allow_replace=True."""
    default_dir = tmp_path / "default"
    overlay_dir = tmp_path / "overlay"
    default_dir.mkdir()
    overlay_dir.mkdir()

    (default_dir / "p1").mkdir()
    (default_dir / "p1" / "manifest.yaml").write_text("name: p1\nallowed_departments: ['*']\n")
    (overlay_dir / "p1").mkdir()
    (overlay_dir / "p1" / "manifest.yaml").write_text("name: p1\nallowed_departments: [hr]\n")
    (overlay_dir / "p2").mkdir()
    (overlay_dir / "p2" / "manifest.yaml").write_text("name: p2\nallowed_departments: ['*']\n")

    registry = PluginRegistry()
    registry.load_directory(default_dir)
    registry.load_directory(overlay_dir, allow_replace=True)

    plugins = {p.manifest.name: p for p in registry.list_all()}
    assert set(plugins) == {"p1", "p2"}
    # Overlay p1 overrides default: allowed_departments is the overlay value.
    assert plugins["p1"].manifest.allowed_departments == ["hr"]


def test_load_extensions_registers_plugin_tools_in_full_registry(
    tmp_path: Path, monkeypatch
) -> None:
    from corpclaw_lite.extensions import bootstrap

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    plugin = Plugin(
        manifest=PluginManifest(
            name="p_tool",
            version="1.0",
            type="plugin",
            description="Plugin",
        ),
        tools=[DummyPluginTool()],
    )

    def fake_load_directory(self: PluginRegistry, path: Path, **kwargs: object) -> None:
        self.register(plugin)

    monkeypatch.setattr(PluginRegistry, "load_directory", fake_load_directory)

    main_registry = ToolRegistry()
    full_registry = ToolRegistry()
    bootstrap.load_extensions(
        Settings(),
        tmp_path,
        main_registry,
        bootstrap.SkillsSettings(),
        full_tool_registry=full_registry,
    )

    assert main_registry.get("plugin_tool") is not None
    assert full_registry.get("plugin_tool") is not None


@pytest.mark.asyncio
async def test_plugin_tool_scope_filters_schema_and_execution(tmp_path: Path, monkeypatch) -> None:
    from corpclaw_lite.extensions import bootstrap

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    plugin = Plugin(
        manifest=PluginManifest(
            name="hr_plugin",
            version="1.0",
            type="plugin",
            description="Plugin",
            allowed_departments=["hr"],
        ),
        tools=[DummyPluginTool()],
    )

    def fake_load_directory(self: PluginRegistry, path: Path, **kwargs: object) -> None:
        self.register(plugin)

    monkeypatch.setattr(PluginRegistry, "load_directory", fake_load_directory)

    registry = ToolRegistry()
    bootstrap.load_extensions(Settings(), tmp_path, registry, bootstrap.SkillsSettings())
    tool = registry.get("plugin_tool")
    assert tool is not None
    assert tool.source_kind == "plugin"
    assert tool.source_name == "hr_plugin"

    mgr = DepartmentManager()
    mgr._departments["engineering"] = DepartmentConfig(
        {
            "description": "Engineering",
            "allowed_tools": ["*"],
            "allowed_plugins": ["*"],
        }
    )
    mgr._departments["hr"] = DepartmentConfig(
        {
            "description": "HR",
            "allowed_tools": ["*"],
            "allowed_plugins": ["*"],
        }
    )
    checker = PermissionChecker(mgr)
    engineering = User(id=1, name="Eng", department="engineering")
    hr = User(id=2, name="HR", department="hr")

    assert registry.to_schemas_for_user(checker, engineering) == []
    assert len(registry.to_schemas_for_user(checker, hr)) == 1

    denied = await registry.execute(
        "plugin_tool",
        {},
        user=engineering,
        permission_checker=checker,
    )
    assert "Permission denied" in denied
    assert await registry.execute("plugin_tool", {}, user=hr, permission_checker=checker) == "ok"


def test_load_extensions_overlay_plugin_overrides_default_tool(tmp_path: Path, monkeypatch) -> None:
    """An overlay plugin with the same name and tool name overrides the default
    plugin's tool in the registry (overlay wins, not default)."""
    from corpclaw_lite.config.settings import ExtensionsSettings
    from corpclaw_lite.extensions import bootstrap

    class DefaultTool(Tool):
        name = "shared_tool"
        description = "default impl"
        params = []

        async def execute(self, **kwargs: object) -> str:
            return "default"

    class OverlayTool(Tool):
        name = "shared_tool"
        description = "overlay impl"
        params = []

        async def execute(self, **kwargs: object) -> str:
            return "overlay"

    default_plugin = Plugin(
        manifest=PluginManifest(
            name="corp",
            version="1.0",
            type="plugin",
            description="default",
            path=Path("default/corp"),
        ),
        tools=[DefaultTool()],
    )
    overlay_plugin = Plugin(
        manifest=PluginManifest(
            name="corp",
            version="1.0",
            type="plugin",
            description="overlay",
            path=Path("overlay/corp"),
        ),
        tools=[OverlayTool()],
    )

    # resolve_dirs("plugins", settings, project_root) returns
    # [project_root/plugins, <overlay>/plugins]. Create the matching dirs.
    project_root = tmp_path / "project"
    overlay_root = tmp_path / "overlay"
    default_plugins_dir = project_root / "plugins"
    overlay_plugins_dir = overlay_root / "plugins"
    default_plugins_dir.mkdir(parents=True)
    overlay_plugins_dir.mkdir(parents=True)

    def fake_load_directory(self: PluginRegistry, path: Path, **kwargs: object) -> None:
        allow_replace = bool(kwargs.get("allow_replace", False))
        if path == default_plugins_dir:
            self.register(default_plugin)
        else:
            self.register(overlay_plugin, allow_replace=allow_replace)

    monkeypatch.setattr(PluginRegistry, "load_directory", fake_load_directory)

    settings = Settings(extensions=ExtensionsSettings(extra_paths=[str(overlay_root)]))
    registry = ToolRegistry()
    bootstrap.load_extensions(settings, project_root, registry, bootstrap.SkillsSettings())

    tool = registry.get("shared_tool")
    assert tool is not None
    # Overlay description wins (overlay registered last with allow_replace=True).
    assert tool.description == "overlay impl"
