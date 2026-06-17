from __future__ import annotations

import logging
from pathlib import Path

from corpclaw_lite.extensions.plugins.base import Plugin
from corpclaw_lite.extensions.plugins.loader import PluginLoader
from corpclaw_lite.users.models import User

__all__ = [
    "PluginRegistry",
]

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Manages loaded complete plugins and access control."""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def load_directory(self, plugins_dir: Path | str, *, allow_replace: bool = False) -> None:
        """Load all valid plugin subdirectories from a main plugins directory.

        When ``allow_replace=True`` (overlay loading), a plugin with the same
        manifest name as an existing one overrides it and a WARN is logged.
        """
        dir_path = Path(plugins_dir)
        if not dir_path.exists() or not dir_path.is_dir():
            logger.warning("Plugins directory not found: %s", dir_path)
            return

        loaded_count = 0
        for sub_dir in dir_path.iterdir():
            if sub_dir.is_dir():
                plugin = PluginLoader.load_plugin(sub_dir)
                if plugin:
                    self.register(plugin, allow_replace=allow_replace)
                    loaded_count += 1

        logger.info("Loaded %d plugins from %s", loaded_count, dir_path)

    def register(self, plugin: Plugin, *, allow_replace: bool = False) -> None:
        if plugin.manifest.name in self._plugins and not allow_replace:
            raise ValueError(f"Plugin '{plugin.manifest.name}' is already registered.")
        if plugin.manifest.name in self._plugins and allow_replace:
            logger.warning(
                "Plugin '%s' overridden by overlay: %s",
                plugin.manifest.name,
                getattr(plugin.manifest, "path", None) or "<unknown>",
            )
        self._plugins[plugin.manifest.name] = plugin

    def get(self, name: str) -> Plugin | None:
        """Get a plugin by name. Alias for get_plugin."""
        return self._plugins.get(name)

    def get_plugin(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def unregister(self, plugin_name: str) -> None:
        """Remove a plugin by name (no-op if not found)."""
        self._plugins.pop(plugin_name, None)

    def list_all(self) -> list[Plugin]:
        return list(self._plugins.values())

    def items(self) -> dict[str, Plugin]:
        """Return a copy of the name→plugin mapping."""
        return dict(self._plugins)

    def get_allowed_plugins(self, user: User) -> list[Plugin]:
        """Return plugins this user has department access to."""
        allowed: list[Plugin] = []
        for plugin in self._plugins.values():
            allowed_depts = plugin.manifest.allowed_departments
            if "*" in allowed_depts or user.department in allowed_depts:
                allowed.append(plugin)
        return allowed
