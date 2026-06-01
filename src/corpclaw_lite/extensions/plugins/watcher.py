"""
PluginHotReloader — watches the plugins/ directory for changes and reloads.

Polls each plugin subdirectory every ``poll_interval`` seconds (default: 10).
Detects new, removed, and changed plugins, then performs targeted
register/unregister operations without restarting the process.

A "change" is detected by comparing the maximum mtime across ALL files in
the plugin directory (manifest.yaml, skill.md, tool.py, etc.), so any
modification to any component triggers a full plugin reload.

Usage (mirrors SkillHotReloader / MCPHotReloader)::

    reloader = PluginHotReloader(plugins_dir, plugin_registry, tool_registry, skill_registry)
    reloader.start()   # background asyncio task
    # ... run bot ...
    reloader.stop()
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from corpclaw_lite.extensions.plugins.loader import PluginLoader
from corpclaw_lite.extensions.plugins.registry import PluginRegistry
from corpclaw_lite.extensions.skills.registry import SkillRegistry
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.extensions.tools.scoped import ScopedTool

__all__ = [
    "PluginHotReloader",
]

logger = logging.getLogger(__name__)


class PluginHotReloader:
    """
    File-mtime watcher for the plugins/ directory.

    Tracks per-plugin state:
    - ``_plugin_tools``: plugin_name → list of registered tool names
    - ``_plugin_skill``: plugin_name → registered skill id (or None)
    - ``_mtimes``: plugin_dir → max mtime across all files in that dir
    - ``_known_dirs``: set of currently tracked plugin directories
    """

    def __init__(
        self,
        plugins_dir: Path | str,
        plugin_registry: PluginRegistry,
        tool_registry: ToolRegistry,
        skill_registry: SkillRegistry,
        poll_interval: float = 10.0,
    ) -> None:
        self._dir = Path(plugins_dir)
        self._plugin_registry = plugin_registry
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry
        self._poll_interval = poll_interval

        self._plugin_tools: dict[str, list[str]] = {}  # plugin_name → [tool_name, ...]
        self._plugin_skill: dict[str, str | None] = {}  # plugin_name → skill_id | None
        self._mtimes: dict[Path, float] = {}  # plugin_dir → max(mtime)
        self._known_dirs: set[Path] = set()

        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("PluginHotReloader started watching: %s", self._dir)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("PluginHotReloader stopped.")

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll indefinitely; reload changed plugins."""
        await self._scan()
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("PluginHotReloader error during scan: %s", e)

    def _max_mtime(self, plugin_dir: Path) -> float:
        """Return the most recent mtime of any file inside plugin_dir."""
        max_mt: float = 0.0
        for f in plugin_dir.iterdir():
            if f.is_file():
                mt = f.stat().st_mtime
                if mt > max_mt:
                    max_mt = mt
        return max_mt

    async def _scan(self) -> None:
        """Detect added/removed/changed plugin directories and apply changes."""
        if not self._dir.exists():
            return

        current_dirs: dict[Path, float] = {}
        for sub in self._dir.iterdir():
            if sub.is_dir() and (sub / "manifest.yaml").exists():
                current_dirs[sub] = self._max_mtime(sub)

        current_paths = set(current_dirs.keys())

        # ── Removed plugins ───────────────────────────────────────────────
        for removed in self._known_dirs - current_paths:
            plugin_name = removed.name
            logger.info("PluginHotReloader: plugin '%s' removed", plugin_name)
            await self._unregister_plugin(plugin_name)

        # ── New plugins ───────────────────────────────────────────────────
        for added in current_paths - self._known_dirs:
            plugin_name = added.name
            logger.info("PluginHotReloader: plugin '%s' added", plugin_name)
            self._load_and_register(added)
            self._mtimes[added] = current_dirs[added]

        # ── Changed plugins ───────────────────────────────────────────────
        for existing in current_paths & self._known_dirs:
            if current_dirs[existing] > self._mtimes.get(existing, 0.0):
                plugin_name = existing.name
                logger.info("PluginHotReloader: plugin '%s' changed, reloading", plugin_name)
                await self._unregister_plugin(plugin_name)
                self._load_and_register(existing)
                self._mtimes[existing] = current_dirs[existing]

        self._known_dirs = current_paths

    async def _unregister_plugin(self, plugin_name: str) -> None:
        """Unregister all tools and skill contributed by this plugin."""
        from corpclaw_lite.extensions.plugins.sandbox_proxy import PluginToolProxy

        for tool_name in self._plugin_tools.pop(plugin_name, []):
            tool = self._tool_registry.get(tool_name)
            if isinstance(tool, PluginToolProxy):
                await tool.kill()
            self._tool_registry.unregister(tool_name)
            logger.debug("PluginHotReloader: unregistered tool '%s'", tool_name)

        skill_id = self._plugin_skill.pop(plugin_name, None)
        if skill_id:
            self._skill_registry.unregister(skill_id)
            logger.debug("PluginHotReloader: unregistered skill '%s'", skill_id)

        self._plugin_registry.unregister(plugin_name)

    def _load_and_register(self, plugin_dir: Path) -> None:
        """Load a plugin and register its tools + skill."""
        plugin = PluginLoader.load_plugin(plugin_dir)
        if not plugin:
            logger.warning("PluginHotReloader: failed to load plugin from '%s'", plugin_dir)
            return

        plugin_name = plugin.manifest.name
        registered_tools: list[str] = []

        for tool in plugin.tools:
            scoped_tool = ScopedTool(
                tool,
                source_kind="plugin",
                source_name=plugin_name,
                allowed_departments=plugin.manifest.allowed_departments,
            )
            try:
                self._tool_registry.register(scoped_tool, allow_replace=True)
                registered_tools.append(scoped_tool.name)
                logger.info(
                    "PluginHotReloader: plugin '%s' registered tool '%s'",
                    plugin_name,
                    scoped_tool.name,
                )
            except Exception as e:
                logger.warning(
                    "PluginHotReloader: plugin '%s' tool '%s' failed to register: %s",
                    plugin_name,
                    scoped_tool.name,
                    e,
                )

        skill_id: str | None = None
        if plugin.skill:
            self._skill_registry.register(plugin.skill)
            skill_id = plugin.skill.id
            logger.info(
                "PluginHotReloader: plugin '%s' registered skill '%s'",
                plugin_name,
                skill_id,
            )

        self._plugin_registry.register(plugin)
        self._plugin_tools[plugin_name] = registered_tools
        self._plugin_skill[plugin_name] = skill_id
