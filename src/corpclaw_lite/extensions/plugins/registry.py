import logging
from pathlib import Path

from corpclaw_lite.extensions.plugins.base import Plugin
from corpclaw_lite.extensions.plugins.loader import PluginLoader
from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Manages loaded complete plugins and access control."""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def load_directory(self, plugins_dir: Path | str) -> None:
        """Load all valid plugin subdirectories from a main plugins directory."""
        dir_path = Path(plugins_dir)
        if not dir_path.exists() or not dir_path.is_dir():
            logger.warning(f"Plugins directory not found: {dir_path}")
            return

        loaded_count = 0
        for sub_dir in dir_path.iterdir():
            if sub_dir.is_dir():
                plugin = PluginLoader.load_plugin(sub_dir)
                if plugin:
                    self.register(plugin)
                    loaded_count += 1

        logger.info(f"Loaded {loaded_count} plugins from {dir_path}")

    def register(self, plugin: Plugin) -> None:
        self._plugins[plugin.manifest.name] = plugin

    def get_plugin(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def list_all(self) -> list[Plugin]:
        return list(self._plugins.values())

    def get_allowed_plugins(self, user: User) -> list[Plugin]:
        """Return plugins this user has department access to."""
        allowed: list[Plugin] = []
        for plugin in self._plugins.values():
            allowed_depts = plugin.manifest.allowed_departments
            if "*" in allowed_depts or user.department in allowed_depts:
                allowed.append(plugin)
        return allowed
