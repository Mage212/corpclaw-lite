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
    File-mtime watcher for one or more plugins/ directories.

    Tracks per-plugin state keyed by the plugin directory Path (not the
    directory basename), so multiple overlay root directories whose plugin
    subdirs share a basename don't clobber each other:
    - ``_plugin_tools``: plugin_dir → list of registered tool names
    - ``_plugin_skill``: plugin_dir → registered skill id (or None)
    - ``_mtimes``: plugin_dir → max mtime across all files in that dir
    - ``_known_dirs``: set of currently tracked plugin directories
    """

    def __init__(
        self,
        plugins_dir: Path | str | list[str | Path],
        plugin_registry: PluginRegistry,
        tool_registry: ToolRegistry,
        skill_registry: SkillRegistry,
        poll_interval: float = 10.0,
    ) -> None:
        if isinstance(plugins_dir, list):
            self._dirs: list[Path] = [Path(d) for d in plugins_dir]
        else:
            self._dirs = [Path(plugins_dir)]
        self._plugin_registry = plugin_registry
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry
        self._poll_interval = poll_interval

        self._plugin_tools: dict[Path, list[str]] = {}  # plugin_dir → [tool_name, ...]
        self._plugin_skill: dict[Path, str | None] = {}  # plugin_dir → skill_id | None
        self._plugin_names: dict[Path, str] = {}  # plugin_dir → manifest name (cached)
        self._mtimes: dict[Path, float] = {}  # plugin_dir → max(mtime)
        self._known_dirs: set[Path] = set()

        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("PluginHotReloader started watching: %s", self._dirs)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("PluginHotReloader stopped.")

    async def reload_now(self) -> None:
        """Trigger an immediate rescan (Etap 4: manual reload button)."""
        await self._scan()

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
        current_dirs: dict[Path, float] = {}
        for root in self._dirs:
            if not root.exists():
                continue
            for sub in root.iterdir():
                if sub.is_dir() and (sub / "manifest.yaml").exists():
                    current_dirs[sub] = self._max_mtime(sub)

        current_paths = set(current_dirs.keys())

        # ── Removed plugins ───────────────────────────────────────────────
        for removed in self._known_dirs - current_paths:
            logger.info("PluginHotReloader: plugin '%s' removed", removed.name)
            await self._unregister_plugin(removed)

        # ── New plugins ───────────────────────────────────────────────────
        for added in current_paths - self._known_dirs:
            logger.info("PluginHotReloader: plugin '%s' added", added.name)
            self._load_and_register(added)
            self._mtimes[added] = current_dirs[added]

        # ── Changed plugins ───────────────────────────────────────────────
        for existing in current_paths & self._known_dirs:
            if current_dirs[existing] > self._mtimes.get(existing, 0.0):
                logger.info("PluginHotReloader: plugin '%s' changed, reloading", existing.name)
                await self._unregister_plugin(existing)
                self._load_and_register(existing)
                self._mtimes[existing] = current_dirs[existing]

        self._known_dirs = current_paths

    async def _unregister_plugin(self, plugin_dir: Path) -> None:
        """Unregister all tools and skill contributed by this plugin."""
        from corpclaw_lite.extensions.plugins.sandbox_proxy import PluginToolProxy

        for tool_name in self._plugin_tools.pop(plugin_dir, []):
            tool = self._tool_registry.get(tool_name)
            if isinstance(tool, PluginToolProxy):
                await tool.kill()
            self._tool_registry.unregister(tool_name)
            logger.debug("PluginHotReloader: unregistered tool '%s'", tool_name)

        skill_id = self._plugin_skill.pop(plugin_dir, None)
        if skill_id:
            self._skill_registry.unregister(skill_id)
            logger.debug("PluginHotReloader: unregistered skill '%s'", skill_id)

        # The plugin registry is keyed by manifest name. Use the name cached at
        # load time — the plugin dir may already be gone from disk (removal
        # detection), so we cannot re-read the manifest here.
        plugin_name = self._plugin_names.pop(plugin_dir, None)
        if plugin_name is not None:
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
            self._skill_registry.register(plugin.skill, allow_replace=True)
            skill_id = plugin.skill.id
            logger.info(
                "PluginHotReloader: plugin '%s' registered skill '%s'",
                plugin_name,
                skill_id,
            )

        self._plugin_registry.register(plugin, allow_replace=True)
        self._plugin_tools[plugin_dir] = registered_tools
        self._plugin_skill[plugin_dir] = skill_id
        self._plugin_names[plugin_dir] = plugin_name
