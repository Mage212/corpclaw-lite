import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from corpclaw_lite.extensions.plugins.base import Plugin, PluginManifest
from corpclaw_lite.extensions.skills.loader import SkillLoader
from corpclaw_lite.extensions.tools.base import Tool

__all__ = [
    "PluginLoader",
]

logger = logging.getLogger(__name__)


class PluginLoader:
    """Loads complete Plugins (manifest.yaml + optional skill.md + optional tool.py)."""

    @classmethod
    def load_manifest(cls, path: Path) -> PluginManifest | None:
        """Load manifest list from `manifest.yaml`."""
        if not path.exists():
            return None

        try:
            with open(path, encoding="utf-8") as f:
                data: dict[str, Any] = yaml.safe_load(f) or {}

            return PluginManifest(
                name=data.get("name", path.parent.name),
                version=data.get("version", "1.0.0"),
                type=data.get("type", "plugin"),
                description=data.get("description", "No description"),
                allowed_departments=data.get("allowed_departments", ["*"]),
                components=data.get("components", {}),
                requires=data.get("requires", {}),
                path=path,
            )
        except Exception as e:
            logger.error("Failed to load plugin manifest %s: %s", path, e)
            return None

    @classmethod
    def load_plugin(cls, plugin_dir: Path, *, force_reload: bool = False) -> Plugin | None:
        """Load an entire plugin directory.

        Args:
            plugin_dir: Path to the plugin directory (must contain manifest.yaml).
            force_reload: If True, clear the importlib module cache before
                re-importing tool.py so that changes are picked up by hot-reload.
        """
        if not plugin_dir.exists() or not plugin_dir.is_dir():
            return None

        manifest_path = plugin_dir / "manifest.yaml"
        manifest = cls.load_manifest(manifest_path)
        if not manifest:
            logger.warning("Plugin directory %s is missing manifest.yaml or invalid.", plugin_dir)
            return None

        plugin_skill = None
        plugin_tools: list[Tool] = []
        plugin_scripts: list[Path] = []

        # Load skill if defined
        skill_filename = manifest.components.get("skill")
        if skill_filename:
            skill_path = (plugin_dir / skill_filename).resolve()
            if not skill_path.is_relative_to(plugin_dir.resolve()):
                logger.error(
                    "Plugin %s: skill path '%s' escapes plugin directory (path traversal blocked).",
                    manifest.name,
                    skill_filename,
                )
                return None
            loaded_skill = SkillLoader.load_from_file(skill_path)
            if loaded_skill:
                plugin_skill = loaded_skill
            else:
                logger.warning(
                    "Plugin %s defined skill %s but failed to load it.",
                    manifest.name,
                    skill_filename,
                )

        # Load tool if defined
        tool_filename = manifest.components.get("tool")
        if tool_filename:
            tool_path = (plugin_dir / tool_filename).resolve()
            if not tool_path.is_relative_to(plugin_dir.resolve()):
                logger.error(
                    "Plugin %s: tool path '%s' escapes plugin directory (path traversal blocked).",
                    manifest.name,
                    tool_filename,
                )
                return None
            if tool_path.exists():
                try:
                    module_name = f"plugin_{manifest.name}_tool"

                    # Clear cached module so hot-reload picks up changed code
                    if force_reload and module_name in sys.modules:
                        del sys.modules[module_name]

                    spec = importlib.util.spec_from_file_location(module_name, tool_path)

                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        sys.modules[module_name] = module
                        spec.loader.exec_module(module)

                        # Find any class that inherits from Tool
                        for attr_name in dir(module):
                            attr = getattr(module, attr_name)
                            if (
                                isinstance(attr, type)
                                and issubclass(attr, Tool)
                                and attr is not Tool
                            ):
                                plugin_tools.append(attr())
                except Exception as e:
                    logger.error("Failed to load tool from plugin %s: %s", manifest.name, e)

        # Load script if defined
        script_filename = manifest.components.get("script")
        if script_filename:
            script_path = plugin_dir / script_filename
            if script_path.exists():
                plugin_scripts.append(script_path)

        return Plugin(
            manifest=manifest,
            skill=plugin_skill,
            tools=plugin_tools,
            scripts=plugin_scripts,
        )
